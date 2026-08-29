"""Minimal Bitbucket Data Center MCP server."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import quote

import httpx
import yaml
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

Cursor = Annotated[int, Field(ge=0)]
Limit = Annotated[int, Field(ge=1, le=100)]
PositiveId = Annotated[int, Field(gt=0)]
ContextLines = Annotated[int, Field(ge=0, le=10_000)]
PrState = Literal["ALL", "OPEN", "MERGED", "DECLINED"]
ReviewStatus = Literal["APPROVED", "NEEDS_WORK", "UNAPPROVED"]
AnchorSide = Literal["destination", "source"]

INITIAL_ANCHOR_CONTEXT = 10
MAX_ANCHOR_CONTEXT = 1_000
MAX_PAGES = 100


class Anchor(BaseModel):
    """A source or destination line in the current effective PR diff."""

    model_config = ConfigDict(extra="forbid")

    path: str
    line: PositiveId
    side: AnchorSide = "destination"


@dataclass(frozen=True)
class BitbucketSettings:
    api_base: str
    token: str
    user_slug: str
    timeout: float


@dataclass(frozen=True)
class ProxySettings:
    server: str
    username: str | None = None
    password: str | None = None


@dataclass(frozen=True)
class Settings:
    bitbucket: BitbucketSettings
    proxy: ProxySettings | None = None


settings: Settings


class ConfigurationError(RuntimeError):
    """Invalid server configuration."""


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{name} must be a non-empty string.")
    return value.strip()


def _required_setting(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{name} must be a non-empty string.")
    return value.strip()


def _value_or_environment(value: Any, name: str) -> str:
    value = _required_setting(value, name)
    if not value.startswith("${"):
        return value
    match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
    if not match:
        raise ConfigurationError(f"{name} must use ${{ENVIRONMENT_VARIABLE}} syntax.")
    variable = match.group(1)
    return _required_setting(os.getenv(variable), variable)


def _load_proxy_settings(value: Any) -> ProxySettings:
    if not isinstance(value, dict):
        raise ConfigurationError("proxy must be a mapping.")

    server = _value_or_environment(value.get("server"), "server")
    username_value = value.get("username")
    password_value = value.get("password")
    if (username_value is None) != (password_value is None):
        raise ConfigurationError("username and password must be configured together.")
    username = (
        _value_or_environment(username_value, "username")
        if username_value is not None
        else None
    )
    password = (
        _value_or_environment(password_value, "password")
        if password_value is not None
        else None
    )

    try:
        parsed = httpx.URL(server)
    except httpx.InvalidURL as exc:
        raise ConfigurationError(
            "server must resolve to an HTTPS URL without credentials, "
            "a query, or a fragment."
        ) from exc
    if (
        parsed.scheme != "https"
        or not parsed.host
        or parsed.userinfo
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError(
            "server must resolve to an HTTPS URL without credentials, "
            "a query, or a fragment."
        )

    return ProxySettings(server, username, password)


def _load_settings() -> Settings:
    config_path = Path(__file__).with_name("config.yml")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise ConfigurationError(
            f"Missing configuration file: {config_path.name}."
        ) from exc
    except OSError as exc:
        raise ConfigurationError(
            f"Cannot read configuration file: {config_path.name}."
        ) from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError("config.yml is not valid YAML.") from exc

    if not isinstance(raw, dict) or not isinstance(raw.get("bitbucket"), dict):
        raise ConfigurationError("config.yml must contain a 'bitbucket' mapping.")
    bitbucket = raw["bitbucket"]

    base_url = _value_or_environment(bitbucket.get("base_url"), "base_url").rstrip("/")
    token = _value_or_environment(bitbucket.get("token"), "token")
    api_prefix = "/" + _value_or_environment(
        bitbucket.get("api_prefix", "/rest/api/1.0"), "api_prefix"
    ).strip("/")
    user_slug = _value_or_environment(bitbucket.get("user_slug"), "user_slug")

    timeout = bitbucket.get("timeout", 30)
    if (
        not isinstance(timeout, int | float)
        or isinstance(timeout, bool)
        or not 0 < timeout <= 300
    ):
        raise ConfigurationError("timeout must be a number between 0 and 300 seconds.")

    try:
        parsed = httpx.URL(base_url)
    except httpx.InvalidURL as exc:
        raise ConfigurationError(
            "base_url must resolve to an HTTP(S) URL without a query or fragment."
        ) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.host
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError(
            "base_url must resolve to an HTTP(S) URL without a query or fragment."
        )
    if parsed.path.rstrip("/").endswith("/dashboard"):
        raise ConfigurationError(
            "base_url must not resolve to the Bitbucket /dashboard path."
        )
    if "://" in api_prefix or any(part == ".." for part in api_prefix.split("/")):
        raise ConfigurationError("api_prefix must be a relative URL path.")

    return Settings(
        bitbucket=BitbucketSettings(
            f"{base_url}{api_prefix}", token, user_slug, float(timeout)
        ),
        proxy=_load_proxy_settings(raw["proxy"]) if "proxy" in raw else None,
    )


class BitbucketAPI:
    def __init__(
        self, settings: BitbucketSettings, proxy: ProxySettings | None = None
    ) -> None:
        self.settings = settings
        self.proxy = proxy
        self.client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        proxy = None
        if self.proxy:
            auth = None
            if self.proxy.username is not None:
                auth = (self.proxy.username, self.proxy.password or "")
            proxy = httpx.Proxy(self.proxy.server, auth=auth)
        self.client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self.settings.token}"},
            timeout=self.settings.timeout,
            follow_redirects=False,
            proxy=proxy,
            trust_env=False,
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self.client:
            await self.client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        accept: str = "application/json",
    ) -> httpx.Response:
        if not self.client:
            raise RuntimeError("BitbucketAPI must be used as an async context manager.")
        try:
            response = await self.client.request(
                method,
                f"{self.settings.api_base}{path}",
                params=params,
                json=json,
                headers={"Accept": accept},
            )
        except httpx.TimeoutException as exc:
            raise ToolError("Bitbucket request timed out.") from exc
        except httpx.HTTPError as exc:
            raise ToolError(
                "Bitbucket request failed before a response was received."
            ) from exc

        if response.is_redirect:
            raise ToolError(
                f"Bitbucket returned an unexpected redirect (HTTP {response.status_code})."
            )
        if response.is_error:
            raise _http_error(response, self.settings.token)
        return response

    async def json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        response = await self.request(method, path, params=params, json=body)
        try:
            return response.json()
        except ValueError as exc:
            raise ToolError("Bitbucket returned invalid JSON.") from exc


async def _request_json(method: str, path: str, **kwargs: Any) -> Any:
    async with BitbucketAPI(settings.bitbucket, settings.proxy) as api:
        return await api.json(method, path, **kwargs)


def _http_error(response: httpx.Response, secret: str | None = None) -> ToolError:
    try:
        errors = response.json().get("errors", [])
    except (AttributeError, ValueError):
        errors = []
    if not isinstance(errors, list):
        errors = []
    messages = [
        item["message"].strip()
        for item in errors
        if isinstance(item, dict)
        and isinstance(item.get("message"), str)
        and item["message"].strip()
    ]

    detail = "; ".join(messages[:3])
    if secret:
        detail = detail.replace(secret, "[REDACTED]")
    message = f"Bitbucket HTTP {response.status_code}"
    if detail:
        message += f": {detail[:500]}"
    if retry_after := response.headers.get("Retry-After"):
        message += f" (Retry-After: {retry_after})"
    return ToolError(message)


def _part(value: str, name: str) -> str:
    value = _required_string(value, name)
    if "/" in value or "\x00" in value:
        raise ToolError(f"{name} must not contain '/'.")
    return quote(value, safe="~")


def _file_path(value: str) -> str:
    value = _required_string(value, "path").replace("\\", "/").strip("/")
    if (
        not value
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ToolError("path must be a normalized repository-relative file path.")
    return value


def _encoded_file_path(value: str) -> str:
    return "/".join(quote(part, safe="") for part in _file_path(value).split("/"))


def _repo_path(project: str, repo: str) -> str:
    return f"/projects/{_part(project, 'project')}/repos/{_part(repo, 'repo')}"


def _pr_path(project: str, repo: str, pr_id: int) -> str:
    return f"{_repo_path(project, repo)}/pull-requests/{pr_id}"


def _branch_ref(branch: str) -> str:
    branch = _required_string(branch, "branch")
    return branch if branch.startswith("refs/heads/") else f"refs/heads/{branch}"


def _compact(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _user(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return _compact(
        slug=value.get("slug") or value.get("name"),
        display_name=value.get("displayName"),
    )


def _ref(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return _compact(
        id=value.get("id"),
        name=value.get("displayId"),
        commit=value.get("latestCommit"),
    )


def _pull_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolError("Bitbucket returned an invalid pull request.")
    return _compact(
        id=value.get("id"),
        title=value.get("title"),
        description=value.get("description"),
        state=value.get("state"),
        draft=value.get("draft"),
        version=value.get("version"),
        author=_user(value.get("author")),
        reviewers=[
            _compact(
                user=_user(item.get("user")),
                status=item.get("status"),
                role=item.get("role"),
                last_reviewed_commit=item.get("lastReviewedCommit"),
            )
            for item in value.get("reviewers", [])
            if isinstance(item, dict)
        ],
        source=_ref(value.get("fromRef")),
        target=_ref(value.get("toRef")),
        created_at=value.get("createdDate"),
        updated_at=value.get("updatedDate"),
    )


def _path_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return None
    text = value.get("toString")
    if isinstance(text, str):
        return text
    components = value.get("components")
    if isinstance(components, list) and all(
        isinstance(part, str) for part in components
    ):
        return "/".join(components)
    parent, name = value.get("parent"), value.get("name")
    if isinstance(name, str):
        return f"{parent}/{name}" if isinstance(parent, str) and parent else name
    return None


def _comment_anchor(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return _compact(
        path=_path_text(value.get("path")),
        source_path=_path_text(value.get("srcPath")),
        line=value.get("line"),
        line_type=value.get("lineType"),
        file_type=value.get("fileType"),
        diff_type=value.get("diffType"),
    )


def _comment(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolError("Bitbucket returned an invalid comment.")
    return _compact(
        id=value.get("id"),
        version=value.get("version"),
        text=value.get("text"),
        author=_user(value.get("author")),
        created_at=value.get("createdDate"),
        updated_at=value.get("updatedDate"),
        resolved=value.get("threadResolved", False),
        anchor=_comment_anchor(value.get("anchor")),
        replies=[
            _comment(item)
            for item in value.get("comments", [])
            if isinstance(item, dict)
        ],
    )


def _participant(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolError("Bitbucket returned an invalid participant.")
    return _compact(
        user=_user(value.get("user")),
        status=value.get("status"),
        role=value.get("role"),
        approved=value.get("approved"),
        last_reviewed_commit=value.get("lastReviewedCommit"),
    )


def _page(value: Any, label: str, current: int) -> tuple[list[Any], int | None]:
    if not isinstance(value, dict) or not isinstance(value.get("values"), list):
        raise ToolError(f"Bitbucket returned an invalid {label} page.")
    if value.get("isLastPage") is True:
        return value["values"], None
    next_cursor = value.get("nextPageStart")
    if (
        not isinstance(next_cursor, int)
        or isinstance(next_cursor, bool)
        or next_cursor <= current
    ):
        raise ToolError(f"Bitbucket {label} pagination did not make progress.")
    return value["values"], next_cursor


def _is_truncated(value: Any) -> bool:
    return value is True or isinstance(value, str) and value.lower() == "true"


def _list_field(value: Any, field: str, error: str) -> list[Any]:
    items = value.get(field) if isinstance(value, dict) else None
    if not isinstance(items, list):
        raise ToolError(f"Bitbucket returned an invalid {error}.")
    return items


def _parse_diff(
    value: Any,
    change: dict[str, Any],
    line: int,
    side: AnchorSide,
) -> tuple[dict[str, Any] | None, set[int]]:
    if isinstance(value, list):
        diffs, truncated = value, False
    else:
        diffs = _list_field(value, "diffs", "structured diff")
        truncated = _is_truncated(value.get("truncated"))

    destination_path = change["path"]
    source_path = change.get("src_path")
    expected_paths = {path for path in (destination_path, source_path) if path}
    number_field = "destination" if side == "destination" else "source"
    allowed_types = (
        {"ADDED", "CONTEXT"} if side == "destination" else {"REMOVED", "CONTEXT"}
    )
    seen: set[int] = set()
    candidates: list[dict[str, Any]] = []
    matched = False

    for raw_diff in diffs:
        if not isinstance(raw_diff, dict):
            raise ToolError("Bitbucket returned an invalid structured diff entry.")
        diff_destination = _path_text(raw_diff.get("destination"))
        diff_source = _path_text(raw_diff.get("source"))
        if expected_paths.isdisjoint(
            {path for path in (diff_destination, diff_source) if path}
        ):
            continue
        matched = True
        if raw_diff.get("binary") is True:
            raise ToolError(
                f"{destination_path} is binary and cannot receive an anchored text comment."
            )
        truncated = truncated or _is_truncated(raw_diff.get("truncated"))
        for hunk in _list_field(raw_diff, "hunks", "diff hunk list"):
            segments = _list_field(hunk, "segments", "diff hunk")
            truncated = truncated or _is_truncated(hunk.get("truncated"))
            for segment in segments:
                lines = _list_field(segment, "lines", "diff segment")
                truncated = truncated or _is_truncated(segment.get("truncated"))
                line_type = str(segment.get("type", "")).upper()
                if line_type not in allowed_types:
                    continue
                for item in lines:
                    if not isinstance(item, dict):
                        raise ToolError("Bitbucket returned an invalid diff line.")
                    truncated = truncated or _is_truncated(item.get("truncated"))
                    number = item.get(number_field)
                    if (
                        not isinstance(number, int)
                        or isinstance(number, bool)
                        or number <= 0
                    ):
                        raise ToolError(
                            f"Bitbucket returned an invalid {side} line number."
                        )
                    seen.add(number)
                    if number == line:
                        anchor = _compact(
                            diffType="EFFECTIVE",
                            path=destination_path,
                            srcPath=(
                                source_path if source_path != destination_path else None
                            ),
                            line=line,
                            lineType=line_type,
                            fileType="TO" if side == "destination" else "FROM",
                        )
                        candidates.append(anchor)

    if not matched:
        raise ToolError(
            f"Bitbucket's structured diff did not contain {destination_path}."
        )
    unique = {tuple(sorted(candidate.items())): candidate for candidate in candidates}
    if len(unique) > 1:
        raise ToolError(
            f"Line {line} in {destination_path} has an ambiguous diff anchor."
        )
    if truncated:
        raise ToolError(
            "Bitbucket truncated the structured diff; the anchor is unsafe."
        )
    return next(iter(unique.values()), None), seen


async def _all_changes(api: BitbucketAPI, pr_path: str) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    cursor = 0
    for _ in range(MAX_PAGES):
        payload = await api.json(
            "GET", f"{pr_path}/changes", params={"start": cursor, "limit": 100}
        )
        values, next_cursor = _page(payload, "pull request changes", cursor)
        for item in values:
            if not isinstance(item, dict):
                raise ToolError("Bitbucket returned an invalid pull request change.")
            path = _path_text(item.get("path"))
            if not path:
                raise ToolError(
                    "Bitbucket returned a change without a destination path."
                )
            changes.append(
                {
                    "path": _file_path(path),
                    "src_path": (
                        _file_path(source_path)
                        if (source_path := _path_text(item.get("srcPath")))
                        else None
                    ),
                    "type": str(item.get("type", "")).upper(),
                }
            )
        if next_cursor is None:
            return changes
        cursor = next_cursor
    raise ToolError(
        "Bitbucket pull request change pagination exceeded the safe page limit."
    )


async def _structured_diff(
    api: BitbucketAPI,
    pr_path: str,
    change: dict[str, Any],
    context_lines: int,
) -> Any:
    return await api.json(
        "GET",
        f"{pr_path}/diff/{_encoded_file_path(change['path'])}",
        params=_compact(
            diffType="EFFECTIVE",
            withComments="false",
            contextLines=context_lines,
            srcPath=change.get("src_path"),
        ),
    )


async def _resolve_anchor(
    api: BitbucketAPI,
    pr_path: str,
    requested: Anchor,
) -> dict[str, Any]:
    requested_path = _file_path(requested.path)
    changes = await _all_changes(api, pr_path)
    matches = [
        change
        for change in changes
        if requested_path == change["path"] or requested_path == change.get("src_path")
    ]
    if not matches:
        raise ToolError(f"{requested_path} is not changed by this pull request.")
    if len(matches) > 1:
        raise ToolError(f"{requested_path} matches more than one pull request change.")
    change = matches[0]
    if requested.side == "destination" and change["type"] == "DELETE":
        raise ToolError("A deleted file has no destination-side anchor.")
    if requested.side == "source" and change["type"] == "ADD":
        raise ToolError("An added file has no source-side anchor.")

    payload = await _structured_diff(api, pr_path, change, INITIAL_ANCHOR_CONTEXT)
    anchor, seen = _parse_diff(payload, change, requested.line, requested.side)
    if anchor:
        return anchor

    distance = min((abs(number - requested.line) for number in seen), default=90)
    expanded_context = INITIAL_ANCHOR_CONTEXT + distance + 1
    if expanded_context > MAX_ANCHOR_CONTEXT:
        raise ToolError(
            "The requested line requires more diff context than can be fetched safely."
        )
    payload = await _structured_diff(api, pr_path, change, expanded_context)
    anchor, _ = _parse_diff(payload, change, requested.line, requested.side)
    if not anchor:
        raise ToolError(
            f"Line {requested.line} has no valid {requested.side}-side diff anchor."
        )
    return anchor


mcp = MCPServer(
    "Bitbucket Data Center",
    instructions="Read and review pull requests. Write tools never merge, decline, reopen, or delete PRs.",
)


@mcp.tool()
async def search_pull_requests(
    project: str,
    repo: str,
    text: str,
    state: PrState = "ALL",
    cursor: Cursor = 0,
    limit: Limit = 25,
) -> dict[str, Any]:
    """Search pull requests by text, including history by default."""
    text = _required_string(text, "text")
    payload = await _request_json(
        "GET",
        f"{_repo_path(project, repo)}/pull-requests",
        params={
            "filterText": text,
            "state": state,
            "start": cursor,
            "limit": limit,
        },
    )
    values, next_cursor = _page(payload, "pull request", cursor)
    return {
        "items": [_pull_request(value) for value in values],
        "next_cursor": next_cursor,
    }


@mcp.tool()
async def get_pull_request(
    project: str, repo: str, pr_id: PositiveId
) -> dict[str, Any]:
    """Read pull request metadata, branches, description, and reviewers."""
    return _pull_request(await _request_json("GET", _pr_path(project, repo, pr_id)))


@mcp.tool(structured_output=False)
async def get_pull_request_diff(
    project: str,
    repo: str,
    pr_id: PositiveId,
    context_lines: ContextLines | None = None,
    whitespace: str | None = None,
) -> str:
    """Read a pull request as compact unified diff text."""
    if whitespace is not None:
        whitespace = _required_string(whitespace, "whitespace")
    params = _compact(contextLines=context_lines, whitespace=whitespace)
    async with BitbucketAPI(settings.bitbucket, settings.proxy) as api:
        response = await api.request(
            "GET",
            f"{_pr_path(project, repo, pr_id)}.diff",
            params=params,
            accept="text/plain",
        )
    text = response.text
    if not text.strip():
        return "No changes."
    truncated = any(
        _is_truncated(response.headers.get(name))
        for name in ("X-Atlassian-Diff-Truncated", "X-Bitbucket-Diff-Truncated")
    )
    return f"[Warning: Bitbucket truncated this diff.]\n{text}" if truncated else text


@mcp.tool()
async def get_pull_request_comments(
    project: str,
    repo: str,
    pr_id: PositiveId,
    cursor: Cursor = 0,
    limit: Limit = 25,
) -> dict[str, Any]:
    """Read pull request discussion threads and replies."""
    payload = await _request_json(
        "GET",
        f"{_pr_path(project, repo, pr_id)}/comments",
        params={"start": cursor, "limit": limit},
    )
    values, next_cursor = _page(payload, "comment", cursor)
    return {
        "comments": [_comment(value) for value in values],
        "next_cursor": next_cursor,
    }


@mcp.tool()
async def create_pull_request(
    project: str,
    repo: str,
    title: str,
    source_branch: str,
    target_branch: str,
    description: str | None = None,
    reviewers: list[str] | None = None,
    draft: bool = False,
) -> dict[str, Any]:
    """Create a same-repository pull request."""
    project_value = _required_string(project, "project")
    repo_value = _required_string(repo, "repo")
    repository = {"slug": repo_value, "project": {"key": project_value}}
    body: dict[str, Any] = {
        "title": _required_string(title, "title"),
        "fromRef": {"id": _branch_ref(source_branch), "repository": repository},
        "toRef": {"id": _branch_ref(target_branch), "repository": repository},
        "draft": draft,
    }
    if description is not None:
        body["description"] = description
    if reviewers is not None:
        body["reviewers"] = [
            {"user": {"name": _required_string(reviewer, "reviewer")}}
            for reviewer in reviewers
        ]
    return _pull_request(
        await _request_json(
            "POST", f"{_repo_path(project, repo)}/pull-requests", body=body
        )
    )


@mcp.tool()
async def add_pull_request_comment(
    project: str,
    repo: str,
    pr_id: PositiveId,
    text: str,
    reply_to: PositiveId | None = None,
    anchor: Anchor | None = None,
) -> dict[str, Any]:
    """Add a general, anchored, or reply comment to a pull request."""
    if reply_to is not None and anchor is not None:
        raise ToolError("reply_to and anchor are mutually exclusive.")
    body = _compact(
        text=_required_string(text, "text"),
        parent={"id": reply_to} if reply_to is not None else None,
    )
    pr_path = _pr_path(project, repo, pr_id)
    async with BitbucketAPI(settings.bitbucket, settings.proxy) as api:
        if anchor is not None:
            body["anchor"] = await _resolve_anchor(api, pr_path, anchor)
        return _comment(await api.json("POST", f"{pr_path}/comments", body=body))


@mcp.tool()
async def set_comment_resolved(
    project: str,
    repo: str,
    pr_id: PositiveId,
    comment_id: PositiveId,
    resolved: bool,
) -> dict[str, Any]:
    """Resolve or unresolve a pull request discussion thread."""
    path = f"{_pr_path(project, repo, pr_id)}/comments/{comment_id}"
    async with BitbucketAPI(settings.bitbucket, settings.proxy) as api:
        current = await api.json("GET", path)
        if not isinstance(current, dict) or not isinstance(current.get("version"), int):
            raise ToolError("Bitbucket returned a comment without a valid version.")
        if current.get("threadResolved", False) is resolved:
            return {"changed": False, "comment": _comment(current)}
        updated = await api.json(
            "PUT",
            path,
            body={"version": current["version"], "threadResolved": resolved},
        )
    return {"changed": True, "comment": _comment(updated)}


@mcp.tool()
async def set_review_status(
    project: str,
    repo: str,
    pr_id: PositiveId,
    status: ReviewStatus,
) -> dict[str, Any]:
    """Set the authenticated user's pull request review status."""
    path = f"{_pr_path(project, repo, pr_id)}/participants/{_part(settings.bitbucket.user_slug, 'user_slug')}"
    async with BitbucketAPI(settings.bitbucket, settings.proxy) as api:
        return _participant(await api.json("PUT", path, body={"status": status}))


def main() -> None:
    global settings
    settings = _load_settings()
    mcp.run()


if __name__ == "__main__":
    main()
