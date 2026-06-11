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

## Out of scope

The following are **not** Threnody vulnerabilities — report them to the vendor:

- Third-party CLI agents (Claude Code, Codex, Copilot, Cursor,
  Junie, OpenCode, etc.)
- Provider account suspension, quota enforcement, or terms-of-service disputes
- Misconfigured provider credentials on the researcher's own machine
- Social engineering or phishing against provider login flows

See [docs/LEGAL.md](docs/LEGAL.md) for provider-policy risk tiers and routing
guidance.

## Deployment notes

### MCP stdio trust model

The MCP server communicates over stdio with the host process that launches it
(Cursor, Claude Code, Copilot, etc.). There is no MCP-layer authentication.
Anyone who can modify the host configuration or spawn the server process has
full access to all MCP tools. Run only on trusted machines with trusted host
shell configurations.

### Dangerous environment variables

- `TGS_AUTO_APPROVE=1` skips the write-approval gate for `execute_subtask`.
  Never set this in shared or production environments.
