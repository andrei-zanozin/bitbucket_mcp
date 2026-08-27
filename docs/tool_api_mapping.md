# Bitbucket MCP Tool–API Mapping

## Target Platform

The MCP server targets Atlassian Bitbucket Data Center 9.4.22. It must not use Bitbucket Cloud APIs.

Configuration:

* Bitbucket base URL will be defined as enviroment variable
* REST API prefix: `/rest/api/1.0`, but it's should be possible to override via an enviroment variable
* Authentication: HTTP access token sent as a Bearer token
* Authenticated user's slug: configured once by the server and not passed to every MCP tool
* The configuration has to be placed at the `config.yml` file at the same directory as a server script

Official references:

* [Bitbucket Data Center 9.4 REST API](https://developer.atlassian.com/server/bitbucket/rest/v904/intro/)
* [Bitbucket Data Center 9.4 pull request API](https://developer.atlassian.com/server/bitbucket/rest/v904/api-group-pull-requests/)
* [HTTP access tokens](https://confluence.atlassian.com/bitbucketserver/personal-access-%20tokens-939515499.html)

## Tool–API Mapping

All repository-scoped tools receive `project` and `repo`. Pull request tools additionally receive `pr_id`.

| MCP tool | Bitbucket Data Center API | Purpose |
| --- | --- | --- |
| `search_pull_requests` | `GET /projects/{project}/repos/{repo}/pull-requests` | Search PRs using `filterText`. Pass `state`, `start`, and `limit`; default `state` to `ALL` so historical PRs are included. |
| `get_pull_request` | `GET /projects/{project}/repos/{repo}/pull-requests/{pr_id}` | Read the title, description, state, author, reviewers, source branch, target branch, latest commits, and resource version. |
| `get_pull_request_diff` | `GET /projects/{project}/repos/{repo}/pull-requests/{pr_id}.diff` | Read the PR as a compact raw unified diff. Support optional context-line and whitespace settings. |
| `get_pull_request_comments` | `GET /projects/{project}/repos/{repo}/pull-requests/{pr_id}/comments` | Read normalized discussion threads, replies, anchors, authors, resolution state, IDs, and versions. |
| `create_pull_request` | `POST /projects/{project}/repos/{repo}/pull-requests` | Create a same-repository PR from a source branch to a target branch. |
| `add_pull_request_comment` | `POST /projects/{project}/repos/{repo}/pull-requests/{pr_id}/comments` | Create a general comment, an anchored comment, or a reply. |
| `set_comment_resolved` | `GET` and `PUT /projects/{project}/repos/{repo}/pull-requests/{pr_id}/comments/{comment_id}` | Resolve or unresolve a discussion using its current resource version. |
| `set_review_status` | `PUT /projects/{project}/repos/{repo}/pull-requests/{pr_id}/participants/{user_slug}` | Set the authenticated reviewer's status to `APPROVED`, `NEEDS_WORK`, or `UNAPPROVED`. |

Use the stable `/rest/api/1.0` prefix when constructing these relative paths. Atlassian's reference often displays `/rest/api/latest`; for this API family, `latest` is an alias for `1.0`.

## Tool Details

### Search Pull Requests

Suggested input:

```json
{
  "project": "PROJECT",
  "repo": "repository",
  "text": "search text",
  "state": "ALL",
  "cursor": 0,
  "limit": 25
}
```

Map `text` to Bitbucket's `filterText` and `cursor` to `start`. The result should contain compact PR summaries and a `next_cursor` derived from `nextPageStart`.

The server must use the returned `nextPageStart` value. It must not calculate the next cursor as `start + size`, because Bitbucket does not guarantee contiguous paging identifiers.

### Get Pull Request

Return only information useful to the client:

* PR ID, title, description, state, and version
* author and reviewers
* `fromRef.id`, `fromRef.displayId`, and `fromRef.latestCommit`
* `toRef.id`, `toRef.displayId`, and `toRef.latestCommit`
* creation and update timestamps

Do not return Bitbucket's complete repository and user objects when their additional fields are not needed.

### Get Pull Request Diff

Use the raw `.diff` endpoint because unified diff text is significantly more compact than Bitbucket's structured diff response and is directly usable by an LLM.

Suggested optional inputs:

* `context_lines`
* `whitespace`: Bitbucket-supported whitespace mode

The tool should identify empty, binary, or server-truncated output explicitly instead of silently returning an incomplete result.

### Get Pull Request Comments

Support `cursor` and `limit`. Normalize each root discussion and its nested replies into a small response containing:

* comment ID and version
* text and author
* creation and update timestamps
* `threadResolved`
* anchor, when present
* nested replies

Preserve unknown comment fields only when they affect discussion behavior. Do not expose Bitbucket's rendered HTML alongside the source text.

### Create Pull Request

Suggested input:

```json
{
  "project": "PROJECT",
  "repo": "repository",
  "title": "PR title",
  "source_branch": "feature/example",
  "target_branch": "develop",
  "description": "Optional description",
  "reviewers": ["reviewer-slug"],
  "draft": false
}
```

The server converts branch names to `refs/heads/{branch}`. If `reviewers` is omitted, let Bitbucket apply its configured default reviewers. Cross-repository PRs are outside the initial scope.

### Add Pull Request Comment

One MCP tool supports three mutually exclusive forms:

* General comment: `text` only; POST `{ "text": "..." }`.
* Reply: `text` and `reply_to`; POST `{ "text": "...", "parent": { "id": reply_to } }`.
* Anchored comment: `text` and `anchor`; resolve the Bitbucket anchor internally before posting.

`reply_to` and `anchor` must not be supplied together.

### Set Comment Resolved

Suggested input:

```json
{
  "project": "PROJECT",
  "repo": "repository",
  "pr_id": 123,
  "comment_id": 456,
  "resolved": true
}
```

Bitbucket requires optimistic locking for comment updates. The server must:

1. Fetch the comment and its current `version`.
2. Return success immediately if `threadResolved` already matches the requested value.
3. PUT `{ "version": current_version, "threadResolved": resolved }`.
4. Report a `409 Conflict` without overwriting a concurrent update.

Use `threadResolved`, not `state`. The `state` field belongs to blocker/task comments and does not represent ordinary discussion-thread resolution.

### Set Review Status

Suggested input:

```json
{
  "project": "PROJECT",
  "repo": "repository",
  "pr_id": 123,
  "status": "APPROVED"
}
```

The server obtains `user_slug` from configuration and sends:

```json
{
  "status": "APPROVED"
}
```

Accepted statuses are:

* `APPROVED`
* `NEEDS_WORK`, shown as "Requested changes" in the Bitbucket UI
* `UNAPPROVED`

This operation changes reviewer status only. The MCP server must not expose merge, decline, reopen, or delete operations as part of this tool.

## Anchored Comment Handling

The public MCP anchor schema should remain small:

```json
{
  "path": "src/example.py",
  "line": 42,
  "side": "destination"
}
```

`side` is optional and defaults to `destination`. The MCP client must not need to know Bitbucket-specific values such as `diffType`, `lineType`, `fileType`, or `srcPath`.

Before posting an anchored comment, the server must:

1. Page through `GET /projects/{project}/repos/{repo}/pull-requests/{pr_id}/changes` to find the requested path and detect renames, deletions, or binary files.
2. Read the path-specific structured diff using `GET /projects/{project}/repos/{repo}/pull-requests/{pr_id}/diff/{path}` with `diffType=EFFECTIVE`, `withComments=false`, and a suitable `contextLines` value.
3. Find exactly one matching diff line and derive the Bitbucket anchor:
   * destination side: `fileType=TO` with `lineType=ADDED` or `CONTEXT`
   * source side: `fileType=FROM` with `lineType=REMOVED` or `CONTEXT`
4. Include `srcPath` for a renamed file when Bitbucket requires it.
5. Reject the operation before POST if the path or line is missing, ambiguous, binary, deleted, or truncated.

The final POST body has this form:

```json
{
  "text": "Review comment",
  "anchor": {
    "diffType": "EFFECTIVE",
    "path": "src/example.py",
    "line": 42,
    "lineType": "ADDED",
    "fileType": "TO"
  }
}
```

Anchor resolution must use the current effective PR diff immediately before the write. The server must not guess a nearby line or silently downgrade an invalid anchored comment to a general comment.

## Authentication and Permissions

Send the configured HTTP access token as:

```text
Authorization: Bearer <token>
```

Use one token per integration and grant the minimum permissions necessary. Repository-write capability is recommended for PR actions, while the associated user must have access to the target repositories. TLS certificate verification remains enabled.

Secrets must never appear in MCP responses or logs.

## Error and Response Handling

* Convert Bitbucket's `errors` array into a concise MCP error without discarding the HTTP status.
* Treat `401` and `403` as authentication or permission failures.
* Treat `404` as an unknown or inaccessible project, repository, PR, comment, path, or line.
* Treat `409` as a version or concurrent-update conflict.
* Preserve Bitbucket rate-limit or retry headers when present.
* Return compact normalized objects rather than the complete upstream response.

Use mocked Bitbucket 9.4 responses for automated tests. Perform live write tests only in a dedicated test repository and PR.
