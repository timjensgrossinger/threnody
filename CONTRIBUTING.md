# Contributing

Threnody requires Python 3.10 or newer. Keep changes focused and follow the
existing module boundaries: shared behavior belongs in `shared/`, while
provider-specific command and model behavior belongs in its adapter.

## Development Setup

```bash
git clone https://github.com/timjensgrossinger/threnody.git
cd threnody
python3 -m pip install pyyaml pytest
```

Do not use your installed runtime configuration as a source file. Copy
`config.example.yaml` to the installed location only when local configuration
is needed.

## Verification

Run focused tests first, then the hermetic suite:

```bash
THRENODY_TEST_MODE=1 python3 -m pytest tests/test_router.py -q
THRENODY_TEST_MODE=1 python3 -m pytest tests/ -q
python3 -m py_compile mcp_server.py shared/router.py
bash -n install.sh shell/*.sh
```

Tests must not depend on locally installed AI CLIs, active subscriptions, or
network access. Add a regression test for every bug fix.

## Pull Requests

- Use a scoped Conventional Commit title such as `fix(router): ...`.
- Explain behavior changes and provider-contract impacts.
- List the exact verification commands run.
- Call out configuration, schema, security, and installer changes.
- Do not commit generated provider inventories, databases, backups, status
  files, credentials, or machine-specific paths.

By contributing, you agree that your contribution is licensed under the Apache
License, Version 2.0.
