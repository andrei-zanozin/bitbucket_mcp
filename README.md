# Bitbucket Data Center MCP Server

A small stdio MCP server for Bitbucket Data Center pull-request workflows. It can
search and read PRs, inspect diffs and comments, create PRs, add or resolve comments,
and set review status.

## Requirements

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- A Bitbucket HTTP access token with permission for the operations you will use

## Configuration

Create `config.yml` next to `bitbucket_mcp.py`:

```yaml
bitbucket:
  user_slug: my-user
  base_url: ${BITBUCKET_BASE_URL}
  token: ${BITBUCKET_TOKEN}
```

Then provide the referenced environment variables:

```bash
export BITBUCKET_BASE_URL="https://bitbucket.example.com"
export BITBUCKET_TOKEN="your-access-token"
```

Use the Bitbucket server root URL, without `/dashboard` or a REST API path.
`base_url`, `token`, and `user_slug` accept either literal values or
`${ENVIRONMENT_VARIABLE}` references. Environment references are preferred,
especially for `token`, to avoid storing credentials in `config.yml`. Set
`user_slug` to the Bitbucket user represented by the token. All three settings are
required and validated before the MCP server starts.

Add optional settings to the same `bitbucket` mapping when needed:

```yaml
bitbucket:
  api_prefix: /rest/api/1.0
  timeout: 30
```

`api_prefix` defaults to `/rest/api/1.0` and may also use an environment reference,
such as `${BITBUCKET_API_PREFIX}`. `timeout` is measured in seconds and must be
greater than `0` and no greater than `300`.

To route Bitbucket requests through an HTTP or HTTPS proxy, add a root-level `proxy`
mapping:

```yaml
proxy:
  server: ${PROXY_SERVER}
  username: ${PROXY_USERNAME}
  password: ${PROXY_PASSWORD}
```

`server` is required when the `proxy` mapping is present and must use an `http://`
or `https://` URL. This scheme describes the connection to the proxy; Bitbucket can
still use HTTPS. Proxy authentication is optional; omit both `username` and
`password` for an anonymous proxy. If authentication is used, both settings are
required. All proxy settings accept literal values or environment references.

## Run

```bash
uv run bitbucket_mcp.py
```

`uv` installs the dependencies declared in the script. Configure your MCP client to
launch this command over stdio and ensure the referenced environment variables are
available to the server process.
