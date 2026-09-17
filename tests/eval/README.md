# Eval fixture quickstart

The eval suite lives under `tests/eval/` and is organized by category directory (`low_tier`, `medium_tier`, `high_tier`, `urgency`, `fanout`). Most fixtures should be tagged `stable`, which means they participate in the normal pass/fail run. Fixtures tagged `boundary` are still useful for exploration, but the runner reports them as `SKIP` and they do not gate the overall exit code.

## 1. Add or update a fixture

Put each fixture in the directory that matches its `category` and follow `tests/eval/schema.json`.

```json
{
  "id": "example-low-rename",
  "category": "low_tier",
  "tags": ["stable", "rename"],
  "prompt": "Rename foo.py to bar.py and show the diff.",
  "expected": {
    "tier": "low",
    "score_min": 0.14,
    "score_max": 0.16,
    "urgency_expected": false,
    "fanout_expected": "favor_parallel"
  }
}
```

Field reminders:

- Required top-level fields: `id`, `category`, `tags`, `prompt`, `expected`
- Required `expected` fields: `tier`, `score_min`, `score_max`
- Allowed `tier` values: `low`, `medium`, `high`
- Categories must match the directory name from `tests/eval/schema.json`
- For local runs, document fanout expectations with the runner-supported values `none` or `favor_parallel`
- Optional top-level fields: `test_mode`, `simulated_result`, `seed_files`

### Fixtures that need file evidence

Routing tier is not a function of the prompt alone: `route_task` resolves the files a task
touches and scores their security risk and size (`shared/risk_signals.py`). A prose-only fixture
cannot exercise that path, so add `seed_files` to give the fixture something to look at:

```json
{
  "id": "evidence-security-defect-01",
  "category": "high_tier",
  "tags": ["stable", "evidence"],
  "prompt": "tidy the helper",
  "seed_files": {
    "store.py": "def q(conn, uid):\n    return conn.execute(f'SELECT * FROM t WHERE id={uid}').fetchall()\n"
  },
  "expected": { "tier": "high", "score_min": 0.36, "score_max": 0.44 }
}
```

The files are written to a throwaway directory and scanned before the prompt is classified.
Content is **inlined in the fixture** rather than pointing at real repo paths on purpose: a
fixture that read the repo would silently change its own expected score every time the file it
names is edited. Paths must be relative and must not contain `..` — the validator rejects
anything that could escape the temp directory and write into the repo.

## 2. Run the suite before you change config or expected behavior

Use the shipped shell wrapper when it is available:

```bash
threnody eval run
```

Useful variants:

```bash
threnody eval run --filter low
threnody eval run --filter low,urgency
threnody eval run --filter high --filter fanout
```

If you are not sourcing `shell/ghc.sh`, use the direct Python fallback:

```bash
python -m shared.routing_eval
python -m shared.routing_eval --filter low,urgency
```

Run the suite before you change `config.yaml`, adjust fixture expectations, or refresh a baseline. That gives you the current pass/fail picture first, and it exercises the same in-process runner that CI-style smoke checks use.

## 3. Capture or refresh the local baseline

When you intentionally want a fresh working snapshot of normalized routing output, run:

```bash
threnody eval baseline
```

Fallback:

```bash
python -m shared.eval_baseline
```

This writes `tests/eval/baseline.json`. The file is local-only, already gitignored, and should never be committed.

## Quick troubleshooting

- `FAIL` with `invalid fixture`: check the JSON shape against `tests/eval/schema.json`
- `SKIP` for a fixture: it is tagged `boundary`
- Unexpected fanout mismatch: compare your fixture with the current runner behavior in `shared/routing_eval.py`
- Baseline questions: inspect `tests/eval/baseline.json` after a successful capture

## Maintainer notes

`threnody eval baseline` and `python -m shared.eval_baseline` write a deterministic snapshot with top-level `config_hash` and `schema_version`, plus normalized per-fixture actuals. The baseline is a local working artifact, not a shared golden file.

Before you open a PR:

1. Run `threnody eval run` (or `python -m shared.routing_eval`) and review failures.
2. Refresh the baseline only when you intentionally want a new local snapshot.

Suggested CI smoke check:

```bash
python -m shared.routing_eval
```

Code references:

- Schema contract: `tests/eval/schema.json`
- Runner behavior and filter parsing: `shared/routing_eval.py`
- Baseline writer: `shared/eval_baseline.py`
- Shell wrapper commands: `shell/ghc.sh`
