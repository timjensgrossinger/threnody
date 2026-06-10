"""
Routing evaluation framework for Threnody.

Phase 26: schema validation stub.
Full runner (route_task integration) implemented in Phase 27.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Any

EVAL_DIR = Path(__file__).parent.parent / "tests" / "eval"
SCHEMA_PATH = EVAL_DIR / "schema.json"
DEFAULT_BASELINE_PATH = EVAL_DIR / "baseline.json"
SWARM_FIXTURE_GLOB = "fixtures_swarm_*.json"

VALID_TIERS = ("low", "medium", "high")
# "favor_linear" not produced by _agents_to_fanout; omitted until a mapping is defined
VALID_FANOUT = ("none", "favor_parallel")
VALID_CATEGORIES = ("low_tier", "medium_tier", "high_tier", "urgency", "fanout")

# Module-level injectable router symbol. Tests can monkeypatch
# shared.routing_eval._TaskRouter before calling run_eval().
_TaskRouter = None

_OPTIONAL_TOP_LEVEL_KEYS = {"test_mode", "simulated_result"}


def _agents_to_fanout(n: int) -> str:
    """Map RoutingDecision.agents (int) to fanout enum per Phase 27 decisions.

    Rules (per D6 & 27-CONTEXT):
    - n <= 1  -> "none"
    - n >= 2  -> "favor_parallel"
    """
    return "favor_parallel" if n >= 2 else "none"


def _validate_fixture(fixture: dict[str, Any]) -> list[str]:
    """Validate a fixture dict against the schema rules without external deps.

    Returns a list of error strings; empty list means valid.
    """
    errors: list[str] = []

    # top-level required keys
    for key in ("id", "category", "tags", "prompt", "expected"):
        if key not in fixture:
            errors.append(f"missing required field: '{key}'")

    if errors:
        return errors

    id_val = fixture["id"]
    if not isinstance(id_val, str) or not id_val:
        errors.append("'id' must be a non-empty string")
    elif not all(c.isalnum() or c in "-_" for c in id_val):
        errors.append(f"'id' contains invalid characters: {id_val!r}")
    elif id_val[0] in "-_":
        errors.append(f"'id' must start with an alphanumeric character: {id_val!r}")

    category = fixture["category"]
    if category not in VALID_CATEGORIES:
        errors.append(f"'category' must be one of {VALID_CATEGORIES}, got {category!r}")

    tags = fixture["tags"]
    if not isinstance(tags, list) or not tags:
        errors.append("'tags' must be a non-empty list")
    elif not all(isinstance(t, str) for t in tags):
        errors.append("'tags' must contain only strings")

    prompt = fixture["prompt"]
    if not isinstance(prompt, str) or len(prompt) < 5:
        errors.append("'prompt' must be a string of at least 5 characters")

    expected = fixture["expected"]
    if not isinstance(expected, dict):
        errors.append("'expected' must be an object")
        return errors

    for req in ("tier", "score_min", "score_max"):
        if req not in expected:
            errors.append(f"'expected' missing required field: '{req}'")

    if "tier" in expected and expected["tier"] not in VALID_TIERS:
        errors.append(f"'expected.tier' must be one of {VALID_TIERS}, got {expected['tier']!r}")

    for score_key in ("score_min", "score_max"):
        if score_key in expected:
            v = expected[score_key]
            if not isinstance(v, (int, float)) or not (0.0 <= v <= 1.0):
                errors.append(f"'expected.{score_key}' must be a float in [0.0, 1.0], got {v!r}")

    if "score_min" in expected and "score_max" in expected:
        if expected["score_min"] > expected["score_max"]:
            errors.append(
                f"'expected.score_min' ({expected['score_min']}) must be <= 'score_max' ({expected['score_max']})"
            )

    if "urgency_expected" in expected and not isinstance(expected["urgency_expected"], bool):
        errors.append("'expected.urgency_expected' must be a boolean")

    if "fanout_expected" in expected and expected["fanout_expected"] not in VALID_FANOUT:
        errors.append(
            f"'expected.fanout_expected' must be one of {VALID_FANOUT}, got {expected['fanout_expected']!r}"
        )

    extra_keys = set(fixture.keys()) - {
        "id",
        "category",
        "tags",
        "prompt",
        "expected",
        *_OPTIONAL_TOP_LEVEL_KEYS,
    }
    if extra_keys:
        errors.append(f"unexpected top-level fields: {sorted(extra_keys)}")

    extra_expected = set(expected.keys()) - {
        "tier", "score_min", "score_max", "urgency_expected", "fanout_expected"
    }
    if extra_expected:
        errors.append(f"unexpected 'expected' fields: {sorted(extra_expected)}")

    return errors


def _fixture_source(path: Path) -> str:
    """Return the stable repo-relative source path used in eval reporting."""
    try:
        return str(path.resolve().relative_to(EVAL_DIR.parent.parent.resolve()))
    except ValueError as exc:
        raise RuntimeError(
            f"Eval fixture path '{path}' must be inside the repository root"
        ) from exc


def load_fixture(path: str | Path) -> dict[str, Any]:
    """Load one fixture JSON file and annotate it with its stable source path."""
    fixture_path = Path(path)
    if not fixture_path.is_absolute():
        fixture_path = (EVAL_DIR.parent.parent / fixture_path).resolve()
    source = _fixture_source(fixture_path)
    try:
        with open(fixture_path, encoding="utf-8") as f:
            fixture = json.load(f)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Failed to parse eval fixture '{source}': "
            f"{exc.msg} (line {exc.lineno}, column {exc.colno})"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"Failed to read eval fixture '{source}': {exc}") from exc

    if not isinstance(fixture, dict):
        raise RuntimeError(
            f"Eval fixture '{source}' must be a JSON object, got {type(fixture).__name__}"
        )

    fixture["_source"] = source
    return fixture


def load_fixtures(category: str | None = None) -> list[dict[str, Any]]:
    """Load all fixture JSON files from tests/eval/.

    Args:
        category: If set, load only fixtures from that category subdirectory.

    Returns:
        List of fixture dicts.
    """
    fixtures: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    dirs = [EVAL_DIR / category] if category else [
        EVAL_DIR / cat for cat in VALID_CATEGORIES
    ]
    for d in dirs:
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.json")):
            fixture = load_fixture(path)
            source = str(fixture.get("_source") or "")
            if source in seen_sources:
                continue
            seen_sources.add(source)
            fixtures.append(fixture)
    for path in sorted(EVAL_DIR.glob(SWARM_FIXTURE_GLOB)):
        fixture = load_fixture(path)
        source = str(fixture.get("_source") or "")
        if source in seen_sources:
            continue
        if category is None or fixture.get("category") == category:
            seen_sources.add(source)
            fixtures.append(fixture)
    return fixtures


# ---------------------------------------------------------------------------
# Comparison & output helpers
# ---------------------------------------------------------------------------

def _compare_fixture(fixture: dict[str, Any], decision: Any) -> tuple[bool, str]:
    """Compare a RoutingDecision against fixture expected values.

    Returns (passed: bool, reason: str). Reason is empty on pass.
    Comparisons run in order (D6): tier → score range → urgency → fanout.
    First failure is returned immediately.
    """
    expected = fixture["expected"]

    if decision.tier != expected["tier"]:
        return False, f"tier={decision.tier} expected={expected['tier']}"

    s_min = expected["score_min"]
    s_max = expected["score_max"]
    if not (s_min <= decision.score <= s_max):
        return False, (
            f"score={decision.score:.2f} expected={s_min:.2f}-{s_max:.2f}"
        )

    if "urgency_expected" in expected:
        actual_urgent = decision.urgency_score > 0
        if actual_urgent != expected["urgency_expected"]:
            return False, (
                f"urgency={actual_urgent} expected={expected['urgency_expected']}"
            )

    if "fanout_expected" in expected:
        actual_fanout = _agents_to_fanout(decision.agents)
        if actual_fanout != expected["fanout_expected"]:
            return False, (
                f"fanout={actual_fanout} expected={expected['fanout_expected']}"
            )

    return True, ""


def _validate_decision_shape(decision: Any) -> str | None:
    """Return an error string when a routing decision is missing required fields."""
    required_attrs = ("tier", "score", "urgency_score", "agents")
    missing: list[str] = []
    for attr in required_attrs:
        try:
            getattr(decision, attr)
        except AttributeError:
            missing.append(attr)
        except Exception as exc:
            return f"attribute access failed for {attr}: {type(exc).__name__}: {exc}"
    if missing:
        return f"missing attributes: {', '.join(missing)}"
    return None


def _normalize_decision(
    fixture_id: str,
    category: str,
    decision: Any,
    *,
    baseline_fixture_id: str | None = None,
    compare_score: bool = True,
) -> dict[str, Any]:
    return {
        "id": fixture_id,
        "category": category,
        "tier": decision.tier,
        "score": float(decision.score),
        "urgency": bool(getattr(decision, "urgency_score", 0) > 0),
        "fanout": _agents_to_fanout(getattr(decision, "agents", 1)),
        "baseline_fixture_id": baseline_fixture_id,
        "compare_score": compare_score,
    }


def _load_baseline_entries(baseline_path: str | Path) -> dict[str, dict[str, Any]]:
    with open(baseline_path, encoding="utf-8") as f:
        payload = json.load(f)
    fixtures = payload.get("fixtures", [])
    if not isinstance(fixtures, list):
        raise RuntimeError("baseline.json must contain a top-level fixtures list")
    entries: dict[str, dict[str, Any]] = {}
    for entry in fixtures:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            entries[entry["id"]] = entry
    return entries


def compare_to_baseline(
    result: dict[str, Any],
    baseline_path: str | Path,
    *,
    baseline_entries: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    """Compare one normalized eval result dict against tests/eval/baseline.json."""
    active_baseline_entries = (
        baseline_entries
        if baseline_entries is not None
        else _load_baseline_entries(baseline_path)
    )
    route_result_raw = result.get("route_task", result)
    if not isinstance(route_result_raw, dict):
        return ["route_task result must be a dict"]

    baseline_fixture_id = route_result_raw.get("baseline_fixture_id") or route_result_raw.get(
        "id"
    )
    if not isinstance(baseline_fixture_id, str) or not baseline_fixture_id:
        return []

    baseline_entry = active_baseline_entries.get(baseline_fixture_id)
    if baseline_entry is None:
        return []

    regressions: list[str] = []
    for field in ("tier", "urgency", "fanout"):
        if route_result_raw.get(field) != baseline_entry.get(field):
            regressions.append(
                f"route_task.{field}={route_result_raw.get(field)!r} baseline={baseline_entry.get(field)!r}"
            )

    compare_score = bool(route_result_raw.get("compare_score", True))
    actual_score = route_result_raw.get("score")
    baseline_score = baseline_entry.get("score")
    if compare_score and isinstance(actual_score, (int, float)) and isinstance(
        baseline_score, (int, float)
    ):
        if abs(float(actual_score) - float(baseline_score)) > 0.05:
            regressions.append(
                f"route_task.score={float(actual_score):.2f} baseline={float(baseline_score):.2f}"
            )

    swarm_result = result.get("swarm")
    if isinstance(swarm_result, dict):
        compatibility = swarm_result.get("compatibility")
        if isinstance(compatibility, dict):
            required_contract_keys = {
                "plan_task": ("analysis", "subtasks", "waves", "strategy"),
                "fleet_plan": ("plan", "fleet_waves", "execution_note"),
                "execute_subtask": ("result", "provider", "model", "tier"),
            }
            for surface, required_keys in required_contract_keys.items():
                actual_surface = compatibility.get(surface)
                if not isinstance(actual_surface, dict):
                    regressions.append(f"compatibility.{surface} must be a dict")
                    continue
                missing = [key for key in required_keys if key not in actual_surface]
                if missing:
                    regressions.append(
                        f"compatibility.{surface} missing keys: {', '.join(missing)}"
                    )

    return regressions


def _evaluate_fixture(
    fixture: dict[str, Any],
    router: Any,
    *,
    baseline_path: str | Path | None = None,
    baseline_entries: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    source = fixture.get("_source", "unknown")
    fixture_data = {k: v for k, v in fixture.items() if k != "_source"}
    fixture_id = fixture_data.get("id", "unknown")
    category = fixture_data.get("category", "unknown")

    errors = _validate_fixture(fixture_data)
    if errors:
        reason = f"invalid fixture ({source}): {'; '.join(errors)}"
        return {
            "status": "fail",
            "id": fixture_id,
            "category": category,
            "reason": reason,
            "regressions": [],
        }

    if "boundary" in fixture_data.get("tags", []):
        return {
            "status": "skip",
            "id": fixture_id,
            "category": category,
            "reason": "(boundary fixture)",
            "regressions": [],
        }

    test_mode_var = fixture_data.get("test_mode")
    if isinstance(test_mode_var, str) and test_mode_var.strip():
        os.environ[test_mode_var.strip()] = "1"

    try:
        decision = router.classify(fixture_data["prompt"])
    except Exception as exc:
        reason = f"classify error: {type(exc).__name__}: {exc}"
        return {
            "status": "fail",
            "id": fixture_id,
            "category": category,
            "reason": reason,
            "regressions": [],
        }

    decision_error = _validate_decision_shape(decision)
    if decision_error is not None:
        reason = f"malformed decision: {decision_error}"
        return {
            "status": "fail",
            "id": fixture_id,
            "category": category,
            "reason": reason,
            "regressions": [],
        }

    try:
        passed, reason = _compare_fixture(fixture_data, decision)
    except Exception as exc:
        reason = f"malformed decision: {type(exc).__name__}: {exc}"
        return {
            "status": "fail",
            "id": fixture_id,
            "category": category,
            "reason": reason,
            "regressions": [],
        }

    result_payload: dict[str, Any] = _normalize_decision(fixture_id, category, decision)
    simulated_result = fixture_data.get("simulated_result")
    if isinstance(simulated_result, dict):
        compatibility = simulated_result.get("compatibility")
        baseline_fixture_id = None
        if isinstance(compatibility, dict):
            candidate = compatibility.get("route_task_baseline_id")
            if isinstance(candidate, str) and candidate.strip():
                baseline_fixture_id = candidate.strip()
        result_payload = {
            "route_task": _normalize_decision(
                fixture_id,
                category,
                decision,
                baseline_fixture_id=baseline_fixture_id,
                compare_score=False,
            ),
            "swarm": deepcopy(simulated_result),
        }

    regressions = (
        compare_to_baseline(
            result_payload,
            baseline_path,
            baseline_entries=baseline_entries,
        )
        if baseline_path is not None
        else []
    )
    status = "pass" if passed and not regressions else "fail"
    if regressions and not reason:
        reason = "; ".join(regressions)
    return {
        "status": status,
        "id": fixture_id,
        "category": category,
        "reason": reason,
        "result": result_payload,
        "regressions": regressions,
    }


def _print_summary(results: list[dict[str, Any]]) -> None:
    """Print the aggregate summary table to stdout."""
    total = len(results)
    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    skipped = sum(1 for r in results if r["status"] == "skip")
    print("=== Eval Summary ===")
    print(f"Total:   {total}")
    print(f"Passed:  {passed}")
    print(f"Failed:  {failed}")
    print(f"Skipped: {skipped}")
    print("By category:")
    for category in sorted({str(r.get("category", "unknown")) for r in results}):
        category_results = [r for r in results if r.get("category") == category]
        category_passed = sum(1 for r in category_results if r["status"] == "pass")
        category_failed = sum(1 for r in category_results if r["status"] == "fail")
        category_skipped = sum(1 for r in category_results if r["status"] == "skip")
        executed = category_passed + category_failed
        accuracy = (category_passed / max(executed, 1)) * 100
        print(
            f"  {category}: pass={category_passed} fail={category_failed} "
            f"skip={category_skipped} executed_accuracy={accuracy:.1f}%"
        )


# ---------------------------------------------------------------------------
# Report formatters (plan 08)
# ---------------------------------------------------------------------------

def format_markdown_report(results: list[dict], date_str: str = "") -> str:
    """Produce a Markdown report from eval results."""
    import datetime
    if not date_str:
        date_str = datetime.date.today().isoformat()
    total = len(results)
    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    skipped = sum(1 for r in results if r["status"] == "skip")
    accuracy = (passed / max(total, 1)) * 100

    lines = [
        f"# Threnody Routing Eval Report",
        f"",
        f"**Date:** {date_str}  ",
        f"**Fixtures:** {total}  ",
        f"**Accuracy:** {accuracy:.1f}%  ",
        f"",
        f"| Status | Count |",
        f"|--------|-------|",
        f"| Pass | {passed} |",
        f"| Fail | {failed} |",
        f"| Skip | {skipped} |",
        f"",
        "## Category Accuracy",
        "",
        "| Category | Pass | Fail | Skip | Executed Accuracy |",
        "|----------|------|------|------|-------------------|",
    ]
    for category in sorted({str(r.get("category", "unknown")) for r in results}):
        category_results = [r for r in results if r.get("category") == category]
        category_passed = sum(1 for r in category_results if r["status"] == "pass")
        category_failed = sum(1 for r in category_results if r["status"] == "fail")
        category_skipped = sum(1 for r in category_results if r["status"] == "skip")
        executed = category_passed + category_failed
        category_accuracy = (category_passed / max(executed, 1)) * 100
        lines.append(
            f"| {category} | {category_passed} | {category_failed} | "
            f"{category_skipped} | {category_accuracy:.1f}% |"
        )
    lines.append("")
    if failed:
        lines += ["## Failures", ""]
        for r in results:
            if r["status"] == "fail":
                lines.append(f"- **{r['id']}** ({r['category']}): {r.get('reason', '')}")
        lines.append("")
    return chr(10).join(lines)


def format_html_report(results: list[dict], date_str: str = "") -> str:
    """Produce a self-contained HTML report from eval results."""
    import datetime
    if not date_str:
        date_str = datetime.date.today().isoformat()
    total = len(results)
    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    skipped = sum(1 for r in results if r["status"] == "skip")
    accuracy = (passed / max(total, 1)) * 100

    rows = ""
    for r in results:
        color = {"pass": "#2d6a2d", "fail": "#8b0000", "skip": "#666"}.get(r["status"], "#333")
        rows += (
            f'<tr><td style="color:{color}">{r["status"].upper()}</td>'
            f'<td>{r.get("category","")}</td>'
            f'<td>{r.get("id","")}</td>'
            f'<td>{r.get("reason","")}</td></tr>'
        )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Threnody Eval {date_str}</title>
<style>body{{font-family:monospace;margin:2em}}table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ccc;padding:6px 12px;text-align:left}}
th{{background:#f0f0f0}}</style></head><body>
<h1>Threnody Routing Eval</h1>
<p><strong>Date:</strong> {date_str} &nbsp;
<strong>Fixtures:</strong> {total} &nbsp;
<strong>Accuracy:</strong> {accuracy:.1f}%</p>
<table><thead><tr><th>Status</th><th>Category</th><th>ID</th><th>Reason</th></tr></thead>
<tbody>{rows}</tbody></table>
<p>Pass: {passed} &nbsp; Fail: {failed} &nbsp; Skip: {skipped}</p>
</body></html>"""


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

_FILTER_ALIASES: dict[str, str] = {
    "low": "low_tier",
    "med": "medium_tier",
    "medium": "medium_tier",
    "high": "high_tier",
    "urgency": "urgency",
    "fanout": "fanout",
}


def run_eval(
    filter_categories: list[str] | None = None,
    *,
    fixtures: list[dict[str, Any]] | None = None,
    baseline_path: str | Path | None = None,
    return_results: bool = False,
    retry_once: bool = False,
) -> int | dict[str, Any]:
    """Run the eval suite in-process.

    Sets THRENODY_TEST_MODE before any router construction to prevent
    real CLI calls.  Returns exit code (0 = all stable pass, 1 = any failure).
    """
    os.environ["THRENODY_TEST_MODE"] = "1"

    # Resolve filter aliases.
    categories: list[str] | None = None
    if filter_categories:
        categories = []
        for raw in filter_categories:
            key = raw.strip().lower()
            if key not in _FILTER_ALIASES:
                print(f"ERROR: unknown filter value: '{raw}'")
                return 1
            categories.append(_FILTER_ALIASES[key])

    try:
        # Lazy-import TaskRouter (after env var is set).
        global _TaskRouter
        if _TaskRouter is None:
            from shared.router import TaskRouter as _TR
            _TaskRouter = _TR

        from shared.config import load_eval_config
        config = load_eval_config()
        router = _TaskRouter(config, db=None)
    except Exception as exc:
        print(f"ERROR: failed to initialize eval router: {type(exc).__name__}: {exc}")
        return 1

    # Load fixtures — filter by category if requested.
    try:
        if categories:
            loaded_fixtures: list[dict[str, Any]] = []
            for cat in categories:
                loaded_fixtures.extend(load_fixtures(category=cat))
        else:
            loaded_fixtures = load_fixtures()
    except Exception as exc:
        print(f"ERROR: failed to load eval fixtures: {type(exc).__name__}: {exc}")
        return {"result": [], "regressions": [str(exc)], "exit_code": 1} if return_results else 1

    active_fixtures = fixtures if fixtures is not None else loaded_fixtures
    if not isinstance(active_fixtures, list):
        print(
            f"ERROR: failed to load eval fixtures: TypeError: expected list, got {type(active_fixtures).__name__}"
        )
        return {"result": [], "regressions": ["fixture input must be a list"], "exit_code": 1} if return_results else 1

    results: list[dict[str, Any]] = []
    baseline_target = baseline_path or DEFAULT_BASELINE_PATH
    active_baseline_path: str | Path | None = None
    baseline_entries: dict[str, dict[str, Any]] | None = None
    if Path(baseline_target).exists():
        active_baseline_path = baseline_target
        baseline_entries = _load_baseline_entries(baseline_target)
    for fixture in active_fixtures:
        if not isinstance(fixture, dict):
            reason = f"invalid fixture entry: expected dict, got {type(fixture).__name__}"
            print(f"FAIL  [unknown] unknown   {reason}")
            results.append({
                "status": "fail",
                "id": "unknown",
                "category": "unknown",
                "reason": reason,
            })
            continue

        evaluation = _evaluate_fixture(
            fixture,
            router,
            baseline_path=active_baseline_path,
            baseline_entries=baseline_entries,
        )
        if retry_once and evaluation["status"] == "fail":
            evaluation = _evaluate_fixture(
                fixture,
                router,
                baseline_path=active_baseline_path,
                baseline_entries=baseline_entries,
            )

        category = evaluation["category"]
        fixture_id = evaluation["id"]
        reason = evaluation.get("reason", "")
        if evaluation["status"] == "pass":
            print(f"PASS  [{category}]  {fixture_id}")
        elif evaluation["status"] == "skip":
            print(f"SKIP  [{category}] {fixture_id}   {reason}")
        else:
            print(f"FAIL  [{category}] {fixture_id}   {reason}")
        results.append(evaluation)

    _print_summary(results)

    any_fail = any(r["status"] == "fail" for r in results)
    if return_results:
        regressions = [
            {
                "id": result["id"],
                "category": result["category"],
                "issues": result.get("regressions", []),
                "reason": result.get("reason", ""),
            }
            for result in results
            if result["status"] == "fail"
        ]
        return {
            "result": results,
            "regressions": regressions,
            "exit_code": 1 if any_fail else 0,
        }
    return 1 if any_fail else 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Run the Threnody routing-correctness eval suite."
    )
    parser.add_argument(
        "--filter",
        dest="filters",
        action="append",
        metavar="CATEGORY",
        help="Filter to a category (low|med|medium|high|urgency|fanout). "
             "Comma-separated or multiple flags.",
    )
    parser.add_argument(
        "--report",
        choices=["html", "markdown"],
        default=None,
        help="Produce a report in the specified format (printed to stdout).",
    )
    args = parser.parse_args()

    # Expand comma-separated values (e.g., "--filter low,urgency").
    filters: list[str] | None = None
    if args.filters:
        filters = []
        for raw in args.filters:
            for part in raw.split(","):
                part = part.strip()
                if part:
                    filters.append(part)

    out = run_eval(filter_categories=filters, return_results=bool(args.report))
    if args.report and isinstance(out, dict):
        results = out.get("result", [])
        if args.report == "html":
            print(format_html_report(results))
        else:
            print(format_markdown_report(results))
        sys.exit(out.get("exit_code", 0))
    sys.exit(out if isinstance(out, int) else out.get("exit_code", 0))
