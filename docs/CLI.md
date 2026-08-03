# Shell Commands

After installation, restart your shell or `source ~/.zshrc`.

## Quick agent calls

```bash
# Orchestrated multi-agent ensemble (auto-decomposes into waves)
ghc agent "implement JWT auth for the user service"

# Quick single-agent calls (auto-routed to cheapest model)
ghcs "how to list files recursively in python"
ghce "what does awk '{print $2}' do"

# Show the plan without executing
ghc agent -w "refactor the database layer"

# Skip orchestration, run single agent
ghc agent --no-plan "add a docstring to this function"

# Cache stats
ghcw
```

## Operator CLI (`threnody`)

```bash
# Inspect router / provider status
threnody inspect status --project .
threnody inspect status --project . --details
threnody inspect task execute-1234

# Review and act on pending approvals
threnody inspect approvals --project .
threnody inspect approvals approve 12 --project . --operator alice
threnody inspect approvals reject 12 --project . --operator alice --reason "too broad"
threnody inspect approvals merge 12 existing-agent-id --project . --operator alice

# Tuning
threnody tune show --project .
threnody tune set concurrency_limit 5 --project .
threnody tune reset concurrency_limit --project .

# Routing eval
threnody eval run
threnody eval run --filter low,urgency
threnody eval baseline

# Provider health
threnody doctor
threnody doctor --repair

# Database maintenance
threnody db check
threnody db backup
```

### Backup retention

`db check` reports `backups_present` and `newest_backup_age_hours`, both read from the
`cache.db.bak.*` files on disk. It deliberately does not report `last_backup_ts`, which
only records a backup the current process took and so reads "never" on a healthy install.

The shipped policy keeps **28 snapshots at a 6h cadence — a 7-day restore window**, tuned
via `cache.backup_keep` / `cache.backup_interval_hours` (see `config.example.yaml`). This
is a learning-durability control: with no snapshot on disk, a corrupt `cache.db` is
quarantined and recreated empty, resetting `review_tier_bias`, `model_quality_events`,
`project_routing` and every other accumulated table.

Retention is a **count, not an age cutoff**. An age-based prune deletes every snapshot once
an install sits idle past the cutoff — dropping the last restore candidate exactly when it
can no longer be regenerated.

## Live monitoring

```bash
threnody-watch
```

Reads `/tmp/threnody-status.json` (written by the MCP server on each subtask).
