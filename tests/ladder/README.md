# Graded task ladder

`tests/eval/` grades whether the router *classified* a task the way a fixture
expected — that measures the classifier against its own opinion. This directory
grades the other half: run a real task at a candidate tier and check
deterministically whether the produced code actually works.

Backing module: `shared/ladder.py`. Operator entry point:

```bash
threnody ladder list [--level 0,1]
threnody ladder run [--tier low,medium,high] [--level N] [--case ID] [--json]
threnody ladder run --no-record          # skip the quality ledger write
```

`run` spends real tokens and needs at least one provider CLI installed. Nothing
here executes on the hot path.

## Layout

```
tests/ladder/L<0-6>/<case-name>/
  case.json          # manifest
  seed/              # files copied into a throwaway sandbox
    <visible files>  # shown to the model as existing code
    test_*.py        # hidden grader — NEVER shown to the model
```

## Manifest

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Unique case id, kebab-case (e.g. `l2-lru-cache`) |
| `level` | yes | Difficulty rung, `0`–`6` |
| `prompt` | yes | The task text sent to the model |
| `target_file` | yes | The single file the model must produce; its output is written here |
| `grader` | no | Command run in the sandbox; exit `0` = pass. Default `python3 -m pytest -q --tb=short` |
| `timeout_seconds` | no | Model execution timeout (default 300) |
| `grader_timeout_seconds` | no | Grader timeout (default 120); a timeout is a failure |

## Levels

| Level | Shape |
|---|---|
| L0 | One-line creation or an off-by-one fix |
| L1 | A single self-contained function with edge cases |
| L2 | A small class with state and eviction/ordering semantics |
| L3 | A parser or algorithm with precedence and error handling |
| L4 | A decorator/higher-order API with metadata and callback contracts |
| L5 | Refactor existing code while preserving exact behavior |
| L6 | Implement against existing modules and hidden invariants |

## Rules that keep this a benchmark

- **Hidden tests stay hidden.** `build_case_prompt()` excludes any seed file whose
  basename starts with `test_`. Leaking the grader would make the run measure
  nothing. `tests/test_ladder.py` asserts this.
- **Every case must be solvable.** `tests/test_ladder.py` holds an inline reference
  solution per case and asserts the grader **accepts** it. Without that, a broken
  grader would silently report every model as failing.
- **Every grader must discriminate.** The same test file asserts the grader
  **rejects** deliberately broken and empty output. Without that, a permissive
  grader would report every model as passing.
- **Not collected by pytest.** `tests/conftest.py` sets
  `collect_ignore_glob = ["ladder/*"]` — the seed `test_*.py` files are graders for
  a sandbox, not tests of this repo.
- **A partial sweep never counts.** `min_passing_tier_by_level()` requires a tier to
  pass *every* attempted case at a level before that level is credited to it.

## Adding a case

1. Create `L<n>/<name>/case.json` and `seed/`.
2. Write the hidden grader as `seed/test_*.py`, asserting behavior the prompt
   actually specifies — including the edge cases, or a cheap tier will pass by luck.
3. Add a reference solution to `REFERENCE` in `tests/test_ladder.py`.
4. Run `python3 -m pytest tests/test_ladder.py -q` — the accept/reject pair for the
   new case must pass before it is trustworthy.

## Where results go

Each verdict is written to the existing `model_quality_events` table with
`source='ladder'`, `dimension='general'`, `sub_dimension='L<n>'`, and a pass/fail
score of 10/0 — so `threnody quality` and `docs/MODEL_QUALITY.md` report it with no
new surface. The derived output that matters is the **minimum passing tier per
model per level**, rendered by `threnody quality` and intended to inform
`preferred_routing` instead of a hand-maintained model mapping.
