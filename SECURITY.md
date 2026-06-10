# Security Policy

## Supported Versions

Threnody is currently pre-release software. Security fixes are applied to
the latest commit on the default branch and to the latest published release
when one exists.

## Reporting a Vulnerability

Do not open a public issue for a suspected vulnerability.

Use GitHub's private vulnerability reporting for this repository. Include:

- Affected version or commit.
- Reproduction steps or a minimal proof of concept.
- Expected and observed impact.
- Any relevant logs with credentials and personal data removed.

You should receive an initial response within seven days. Please allow time for
the issue to be reproduced and fixed before public disclosure.

## Security Boundaries

Threnody launches locally installed AI CLIs, reads local configuration, and
may write files requested by an MCP client. Provider CLIs and configured
verification commands execute with the permissions of the current user.

Never commit credentials to this repository or place credentials in
`config.example.yaml`. Runtime tokens belong in provider-native credential
stores or an untracked local `config.yaml`.

## Deployment notes

### MCP stdio trust model

The MCP server communicates over stdio with the host process that launches it
(Cursor, Claude Code, Copilot, etc.). There is no MCP-layer authentication.
Anyone who can modify the host configuration or spawn the server process has
full access to all MCP tools. Run only on trusted machines with trusted host
shell configurations.

### Remote server (`threnody serve`) — deprecated

`threnody serve`, `remote_dispatch`, and `remote_job_status` are **deprecated
and unsupported** in this release line. Prefer local MCP stdio only. If you must
run the legacy HTTP server:

- Bind to `127.0.0.1` unless you explicitly need LAN/WAN exposure
- Set a stable `THRENODY_SERVER_TOKEN` before sharing access; do not rely on
  auto-generated tokens printed to stdout
- Prefer TLS (`--tls-cert` / `--tls-key`) for non-loopback deployments
- Create user tokens only after the admin token is configured

See [docs/LEGAL.md](docs/LEGAL.md) for provider-policy implications of shared
remote routing.

### Dangerous environment variables

- `TGS_AUTO_APPROVE=1` skips the write-approval gate for `execute_subtask`.
  Never set this in shared or production environments.
