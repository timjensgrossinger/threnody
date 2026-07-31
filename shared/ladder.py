"""Graded task ladder — ground truth for which tier can actually do the work.

``shared/routing_eval.py`` grades whether the router *classified* a task the way a
fixture expected. That validates the classifier against its own opinion, not
against reality. This module grades the other half: run a real task at a candidate
tier and check deterministically whether the produced code works.

Each case (``tests/ladder/L<n>/<name>/``) is a self-contained sandbox: seed files,
a prompt, one target file the model must produce, and a grader command whose exit
status is the verdict. Levels run L0 (a one-line edit) to L6 (a multi-file change
against hidden tests), so a tier's *ceiling* becomes observable rather than assumed.

Results land in the existing ``model_quality_events`` table with
``source='ladder'`` and ``sub_dimension='L<n>'``, which means ``threnody quality``
and ``docs/MODEL_QUALITY.md`` immediately report them alongside the other signals —
no new surface. The derived output that matters is
:func:`min_passing_tier_by_level`: the cheapest tier observed to pass each level,
per model. That is the auto-detected "which model is good at what", and it is meant
to inform ``preferred_routing`` instead of a hand-maintained mapping.

Operator-invoked only (``threnody ladder run``): it spends real tokens and needs at
least one provider CLI installed. Nothing here runs on the hot path.
"""

from __future__ import annotations

import json
import logging
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, NamedTuple

if TYPE_CHECKING:
    from shared.config import TGsConfig
    from shared.db import Database

log = logging.getLogger(__name__)

LADDER_DIR = Path(__file__).parent.parent / "tests" / "ladder"
TIERS = ("low", "medium", "high")
_TIER_RANK = {tier: idx for idx, tier in enumerate(TIERS)}

DEFAULT_GRADER = "python3 -m pytest -q --tb=short"
DEFAULT_TIMEOUT = 300
DEFAULT_GRADER_TIMEOUT = 120

# Score is pass/fail by design: a benchmark that partially credits broken code is
# not ground truth. 10 = the grader accepted it, 0 = it did not.
SCORE_PASS = 10.0
SCORE_FAIL = 0.0


class LadderCase(NamedTuple):
    case_id: str
    level: int
    prompt: str
    target_file: str
    grader: str
    seed: dict[str, str]
    timeout_seconds: int
    grader_timeout_seconds: int
    source: str

    @property
    def level_label(self) -> str:
        return f"L{self.level}"


@dataclass
class LadderResult:
    case_id: str
    level: int
    tier: str
    passed: bool
    model: str = ""
    provider: str = ""
    effort: str | None = None
    error: str = ""
    grader_output: str = ""
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "level": self.level,
            "tier": self.tier,
            "passed": self.passed,
            "model": self.model,
            "provider": self.provider,
            "effort": self.effort,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


@dataclass
class ExecutionOutput:
    """What an executor returns for one (case, tier) attempt."""

    content: str = ""
    model: str = ""
    provider: str = ""
    effort: str | None = None
    error: str = ""


# ---------------------------------------------------------------------------
# Case loading
# ---------------------------------------------------------------------------


def _read_seed(case_dir: Path) -> dict[str, str]:
    seed_dir = case_dir / "seed"
    if not seed_dir.is_dir():
        return {}
    out: dict[str, str] = {}
    for path in sorted(seed_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(seed_dir).as_posix()
        # Skip build artifacts: a stray __pycache__ from an accidental import must
        # not become part of the case, and reading a .pyc as UTF-8 would raise.
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        try:
            out[rel] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            log.warning("ladder: skipping unreadable seed file %s", path)
    return out


def load_case(case_dir: str | Path) -> LadderCase:
    """Load one case directory. Raises RuntimeError on a malformed case."""
    case_dir = Path(case_dir)
    manifest = case_dir / "case.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"ladder: bad case manifest {manifest}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"ladder: case manifest {manifest} must be an object")

    for required in ("id", "level", "prompt", "target_file"):
        if not payload.get(required) and payload.get(required) != 0:
            raise RuntimeError(f"ladder: case {manifest} missing '{required}'")
    try:
        level = int(payload["level"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"ladder: case {manifest} has a non-integer level") from exc
    if not 0 <= level <= 6:
        raise RuntimeError(f"ladder: case {manifest} level must be 0-6, got {level}")

    return LadderCase(
        case_id=str(payload["id"]),
        level=level,
        prompt=str(payload["prompt"]),
        target_file=str(payload["target_file"]),
        grader=str(payload.get("grader") or DEFAULT_GRADER),
        seed=_read_seed(case_dir),
        timeout_seconds=int(payload.get("timeout_seconds") or DEFAULT_TIMEOUT),
        grader_timeout_seconds=int(
            payload.get("grader_timeout_seconds") or DEFAULT_GRADER_TIMEOUT
        ),
        source=str(case_dir),
    )


def load_cases(
    root: str | Path = LADDER_DIR,
    *,
    levels: "Iterable[int] | None" = None,
    case_ids: "Iterable[str] | None" = None,
) -> list[LadderCase]:
    """Load all cases under ``root``, optionally filtered by level or id."""
    root = Path(root)
    if not root.is_dir():
        return []
    wanted_levels = {int(v) for v in levels} if levels is not None else None
    wanted_ids = {str(v) for v in case_ids} if case_ids is not None else None
    cases: list[LadderCase] = []
    for manifest in sorted(root.glob("L*/*/case.json")):
        case = load_case(manifest.parent)
        if wanted_levels is not None and case.level not in wanted_levels:
            continue
        if wanted_ids is not None and case.case_id not in wanted_ids:
            continue
        cases.append(case)
    cases.sort(key=lambda c: (c.level, c.case_id))
    return cases


# ---------------------------------------------------------------------------
# Sandbox + grading
# ---------------------------------------------------------------------------


def materialize(case: LadderCase, dest: str | Path) -> Path:
    """Write the case's seed files into ``dest``. Returns the sandbox path."""
    sandbox = Path(dest)
    sandbox.mkdir(parents=True, exist_ok=True)
    for rel, content in case.seed.items():
        target = sandbox / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return sandbox


def _strip_code_fence(text: str) -> str:
    """Remove a surrounding markdown fence, which models add even when told not to."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    lines = stripped.splitlines()
    if len(lines) < 2:
        return text
    body = lines[1:]
    if body and body[-1].strip().startswith("```"):
        body = body[:-1]
    return "\n".join(body) + "\n"


def grade(case: LadderCase, sandbox: str | Path) -> tuple[bool, str]:
    """Run the case's grader in ``sandbox``. Returns ``(passed, output)``.

    Exit status is the entire verdict — a grader either accepts the produced code
    or it does not. A missing or unparseable grader command counts as a failure of
    the case setup, never as a pass.
    """
    try:
        args = shlex.split(case.grader)
        if not args:
            return False, "grader command is empty"
    except ValueError as exc:
        return False, f"grader command is unparseable: {exc}"
    try:
        proc = subprocess.run(
            args,
            cwd=str(sandbox),
            capture_output=True,
            text=True,
            timeout=case.grader_timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return False, f"grader timed out after {case.grader_timeout_seconds}s"
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"grader failed to run: {exc}"
    output = f"{proc.stdout}\n{proc.stderr}".strip()
    return proc.returncode == 0, output[-4000:]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def build_case_prompt(case: LadderCase) -> str:
    """Render the prompt sent to the model: task, existing files, output contract."""
    parts = [case.prompt.strip(), ""]
    visible = {
        rel: content
        for rel, content in case.seed.items()
        if not Path(rel).name.startswith("test_")
    }
    if visible:
        parts.append("Existing files:")
        for rel, content in sorted(visible.items()):
            parts.append(f"\n--- {rel} ---\n{content}")
        parts.append("")
    parts.append(
        f"Write the complete contents of {case.target_file}. "
        "Output ONLY the file contents — no prose, no markdown fences, no commentary."
    )
    return "\n".join(parts)


def default_executor(
    case: LadderCase, tier: str, *, config: "TGsConfig | None" = None
) -> ExecutionOutput:
    """Execute one case at ``tier`` via the provider registry's cheapest route.

    Uses ``code_only=True`` so providers emit raw source instead of agentic prose.
    Any failure is returned as an error rather than raised, so one dead provider
    does not abort the whole ladder run.
    """
    try:
        from .discovery import get_registry

        registry = get_registry()
        result = registry.execute_cheapest(
            build_case_prompt(case),
            tier=tier,
            timeout=case.timeout_seconds,
            code_only=True,
        )
    except Exception as exc:
        return ExecutionOutput(error=f"{type(exc).__name__}: {exc}")
    if not isinstance(result, dict):
        return ExecutionOutput(error="provider returned a non-dict result")
    content = str(result.get("result") or "")
    if not content.strip():
        return ExecutionOutput(
            error=str(result.get("error") or "provider returned empty output"),
            model=str(result.get("model") or ""),
            provider=str(result.get("provider") or ""),
        )
    return ExecutionOutput(
        content=content,
        model=str(result.get("model") or ""),
        provider=str(result.get("provider") or ""),
        effort=(str(result.get("effort")) if result.get("effort") else None),
    )


Executor = Callable[[LadderCase, str], ExecutionOutput]


def run_case(
    case: LadderCase,
    tier: str,
    *,
    executor: "Executor | None" = None,
    config: "TGsConfig | None" = None,
    keep_sandbox: Path | None = None,
) -> LadderResult:
    """Run one case at one tier in a throwaway sandbox and grade the result."""
    started = time.monotonic()
    run = executor or (lambda c, t: default_executor(c, t, config=config))
    tmpdir = keep_sandbox or Path(tempfile.mkdtemp(prefix="threnody-ladder-"))
    try:
        sandbox = materialize(case, tmpdir)
        output = run(case, tier)
        if output.error or not output.content.strip():
            return LadderResult(
                case_id=case.case_id, level=case.level, tier=tier, passed=False,
                model=output.model, provider=output.provider, effort=output.effort,
                error=output.error or "empty output",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        target = sandbox / case.target_file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_strip_code_fence(output.content), encoding="utf-8")
        passed, grader_output = grade(case, sandbox)
        return LadderResult(
            case_id=case.case_id, level=case.level, tier=tier, passed=passed,
            model=output.model, provider=output.provider, effort=output.effort,
            grader_output="" if passed else grader_output,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    finally:
        if keep_sandbox is None:
            shutil.rmtree(str(tmpdir), ignore_errors=True)


def run_ladder(
    *,
    cases: "list[LadderCase] | None" = None,
    tiers: "Iterable[str]" = TIERS,
    executor: "Executor | None" = None,
    config: "TGsConfig | None" = None,
    db: "Database | None" = None,
    levels: "Iterable[int] | None" = None,
    case_ids: "Iterable[str] | None" = None,
) -> list[LadderResult]:
    """Run every (case x tier) combination, recording each verdict in the ledger."""
    resolved = cases if cases is not None else load_cases(levels=levels, case_ids=case_ids)
    ordered_tiers = [t for t in tiers if t in _TIER_RANK]
    results: list[LadderResult] = []
    for case in resolved:
        for tier in ordered_tiers:
            result = run_case(case, tier, executor=executor, config=config)
            results.append(result)
            log.info(
                "ladder %s %s tier=%s -> %s",
                case.level_label, case.case_id, tier,
                "PASS" if result.passed else "FAIL",
            )
            if db is not None:
                record_ladder_result(db, result)
    return results


def record_ladder_result(db: "Database", result: LadderResult) -> None:
    """Write one graded verdict into ``model_quality_events`` (source='ladder')."""
    try:
        from . import model_quality

        model_quality.record_ladder_score(
            db,
            model=result.model or None,
            effort=result.effort,
            level_label=f"L{result.level}",
            passed=result.passed,
            tier=result.tier,
            case_id=result.case_id,
        )
    except Exception:  # pragma: no cover - best-effort ledger write
        log.debug("ladder: ledger write failed for %s", result.case_id, exc_info=True)


# ---------------------------------------------------------------------------
# Derived: minimum passing tier
# ---------------------------------------------------------------------------


def min_passing_tier_by_level(results: "Iterable[LadderResult]") -> dict[int, str]:
    """Cheapest tier that passed every attempted case at each level.

    Requiring *all* cases at a level to pass is deliberate: one lucky pass out of
    three is not evidence a tier handles that level. Levels where no tier swept are
    simply absent rather than reported optimistically.
    """
    attempted: dict[tuple[int, str], set[str]] = {}
    passed: dict[tuple[int, str], set[str]] = {}
    for result in results:
        key = (result.level, result.tier)
        attempted.setdefault(key, set()).add(result.case_id)
        if result.passed:
            passed.setdefault(key, set()).add(result.case_id)
    out: dict[int, str] = {}
    for level in sorted({lvl for lvl, _ in attempted}):
        for tier in TIERS:
            key = (level, tier)
            if key not in attempted:
                continue
            if passed.get(key, set()) == attempted[key]:
                out[level] = tier
                break
    return out


def summarize(results: "list[LadderResult]") -> dict[str, Any]:
    """Operator-facing rollup: per-tier pass rate plus the min-passing-tier map."""
    by_tier: dict[str, dict[str, int]] = {}
    for result in results:
        bucket = by_tier.setdefault(result.tier, {"passed": 0, "total": 0})
        bucket["total"] += 1
        if result.passed:
            bucket["passed"] += 1
    return {
        "total_runs": len(results),
        "by_tier": {
            tier: {
                **counts,
                "pass_rate": round(counts["passed"] / counts["total"], 3)
                if counts["total"]
                else 0.0,
            }
            for tier, counts in sorted(by_tier.items(), key=lambda kv: _TIER_RANK.get(kv[0], 9))
        },
        "min_passing_tier_by_level": {
            f"L{level}": tier for level, tier in min_passing_tier_by_level(results).items()
        },
        "results": [r.to_dict() for r in results],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_int_list(raw: str | None) -> list[int] | None:
    if not raw:
        return None
    out: list[int] = []
    for token in raw.split(","):
        token = token.strip().lstrip("Ll")
        if token:
            out.append(int(token))
    return out or None


def _parse_str_list(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    out = [t.strip() for t in raw.split(",") if t.strip()]
    return out or None


def render_summary(summary: dict[str, Any]) -> str:
    lines = [f"Ladder runs: {summary['total_runs']}", ""]
    lines.append(f"{'tier':<8} {'passed':>7} {'total':>6} {'rate':>7}")
    for tier, counts in summary["by_tier"].items():
        lines.append(
            f"{tier:<8} {counts['passed']:>7} {counts['total']:>6} {counts['pass_rate']:>7.1%}"
        )
    mins = summary["min_passing_tier_by_level"]
    lines.append("")
    if mins:
        lines.append("Minimum passing tier per level:")
        for level, tier in sorted(mins.items()):
            lines.append(f"  {level}: {tier}")
    else:
        lines.append("No level was swept cleanly by any tier.")
    failures = [r for r in summary["results"] if not r["passed"]]
    if failures:
        lines.append("")
        lines.append("Failures:")
        for r in failures:
            detail = f" ({r['error']})" if r["error"] else ""
            lines.append(f"  L{r['level']} {r['case_id']} @ {r['tier']}{detail}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="threnody ladder",
        description="Run the graded task ladder and record ground-truth model quality.",
    )
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="Run cases at one or more tiers")
    run_p.add_argument("--tier", default=",".join(TIERS), help="Comma list: low,medium,high")
    run_p.add_argument("--level", default=None, help="Comma list of levels, e.g. 0,1,2")
    run_p.add_argument("--case", default=None, help="Comma list of case ids")
    run_p.add_argument("--json", action="store_true", help="Print the JSON summary")
    run_p.add_argument(
        "--no-record", action="store_true", help="Do not write to the quality ledger"
    )

    list_p = sub.add_parser("list", help="List available cases")
    list_p.add_argument("--level", default=None, help="Comma list of levels")

    args = parser.parse_args(argv)
    command = args.command or "list"

    if command == "list":
        cases = load_cases(levels=_parse_int_list(getattr(args, "level", None)))
        if not cases:
            print(f"no ladder cases found under {LADDER_DIR}")
            return 0
        for case in cases:
            print(f"{case.level_label:<3} {case.case_id:<28} -> {case.target_file}")
        return 0

    tiers = _parse_str_list(args.tier) or list(TIERS)
    unknown = [t for t in tiers if t not in _TIER_RANK]
    if unknown:
        print(f"ERROR: unknown tier(s): {', '.join(unknown)}")
        return 1
    cases = load_cases(
        levels=_parse_int_list(args.level), case_ids=_parse_str_list(args.case)
    )
    if not cases:
        print("ERROR: no matching ladder cases")
        return 1

    try:
        from .config import TGsConfig

        config = TGsConfig.from_yaml()
    except Exception:
        config = None
    db = None
    if not args.no_record:
        try:
            from .db_client import open_database

            db = open_database()
        except Exception as exc:
            print(f"WARNING: quality ledger unavailable ({exc}); running without recording")

    results = run_ladder(cases=cases, tiers=tiers, config=config, db=db)
    summary = summarize(results)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(render_summary(summary))
    return 0 if all(r.passed for r in results) else 1


__all__ = [
    "LADDER_DIR",
    "TIERS",
    "LadderCase",
    "LadderResult",
    "ExecutionOutput",
    "load_case",
    "load_cases",
    "materialize",
    "grade",
    "build_case_prompt",
    "default_executor",
    "run_case",
    "run_ladder",
    "record_ladder_result",
    "min_passing_tier_by_level",
    "summarize",
    "render_summary",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
