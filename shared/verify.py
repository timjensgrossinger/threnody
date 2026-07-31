"""Verify gate: lint/type/test signals, graded against the merge base.

One implementation shared by both execution paths. The subprocess orchestrator
already ran these signals per file-writing subtask; the host-native path had no
gate at all. Both now go through here.

The important addition is the **baseline diff**. Running `pytest` after an edit and
blocking on a non-zero exit conflates two very different things: a regression the
agent just introduced, and a test that was already red on the branch. Comparing the
failure *set* against the same signals run at the merge base separates them, so only
genuinely new failures count. That is what makes the gate safe to enable by
default and what makes "clean run" a trustworthy learning signal.

Everything is best-effort and read-only with respect to the working tree: the
baseline runs in a detached `git worktree`, never by mutating the checkout. A
missing git repo, an unavailable tool, or a timeout degrades to "no baseline" and
the gate falls back to plain pass/fail rather than guessing.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from shared.config import VerifyGateConfig

log = logging.getLogger(__name__)

VERDICT_PASS = "pass"
VERDICT_WARN = "warn"
VERDICT_REJECTED = "rejected"

# Bound the baseline run so a slow suite cannot stall finalize indefinitely.
BASELINE_TIMEOUT_MULTIPLIER = 1.0
_GIT_TIMEOUT = 30


@dataclass
class SignalOutcome:
    """Result of one verify signal."""

    name: str
    passed: bool = True
    skipped: bool = False
    unavailable: bool = False
    timed_out: bool = False
    returncode: int | None = None
    command: list[str] = field(default_factory=list)
    failures: set[str] = field(default_factory=set)
    error: str = ""
    output: str = ""
    timeout_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Shape kept compatible with the orchestrator's historical gate_signals."""
        out: dict[str, Any] = {"passed": self.passed}
        if self.skipped:
            out["skipped"] = True
        if self.unavailable:
            out["unavailable"] = True
        if self.timed_out:
            out["timed_out"] = True
            if self.timeout_seconds is not None:
                out["timeout_seconds"] = self.timeout_seconds
        if self.returncode is not None:
            out["returncode"] = self.returncode
        if self.command:
            out["command"] = self.command
        if self.error:
            out["error"] = self.error
        if not self.passed and self.output:
            out["stderr"] = self.output[:500]
        if self.failures:
            out["failures"] = sorted(self.failures)[:50]
        return out


@dataclass
class VerifyReport:
    """Aggregate gate outcome, with pre-existing failures separated out."""

    verdict: str = VERDICT_PASS
    signals: dict[str, dict[str, Any]] = field(default_factory=dict)
    new_failures: list[str] = field(default_factory=list)
    preexisting_failures: list[str] = field(default_factory=list)
    baseline_ref: str | None = None
    baseline_used: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "signals": self.signals,
            "new_failures": self.new_failures,
            "preexisting_failures": self.preexisting_failures,
            "baseline_ref": self.baseline_ref,
            "baseline_used": self.baseline_used,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Command detection
# ---------------------------------------------------------------------------


def detect_gate_command(signal: str, project_root: str) -> str:
    """Auto-detect the command for a signal from what is installed.

    Kept identical to the orchestrator's historical detection so existing
    subprocess-path behavior is unchanged by the extraction.
    """
    if signal == "lint":
        if shutil.which("ruff") is not None:
            return "ruff check ."
        if shutil.which("flake8") is not None:
            return "flake8 ."
        return ""
    if signal == "types":
        if shutil.which("mypy") is not None:
            return "mypy ."
        if shutil.which("pyright") is not None:
            return "pyright ."
        return ""
    if signal == "tests":
        if shutil.which("pytest") is not None:
            return "python3 -m pytest --tb=no -q"
        return ""
    return ""


# ---------------------------------------------------------------------------
# Failure extraction
# ---------------------------------------------------------------------------

# pytest -q: "FAILED tests/test_x.py::test_y - AssertionError: ..."
_PYTEST_FAILED = re.compile(r"^(?:FAILED|ERROR)\s+(\S+?)(?:\s+-\s+.*)?$", re.MULTILINE)
# ruff / flake8 / mypy / pyright: "path:line[:col]: code message"
_LINTER_LINE = re.compile(
    r"^(?P<path>[^\s:][^:]*):(?P<line>\d+)(?::\d+)?:\s*(?P<rest>.+)$", re.MULTILINE
)
# Leading error code, e.g. "E501 line too long" or "error: Incompatible types"
_LINT_CODE = re.compile(r"^(?:(?P<code>[A-Z]+\d+|error|warning|note)\b)?\s*(?P<msg>.*)$")


def extract_failures(signal: str, text: str) -> set[str]:
    """Parse tool output into stable, line-number-independent failure identities.

    Line numbers are deliberately excluded: an unrelated insertion above a warning
    shifts every line below it, which would otherwise make untouched pre-existing
    findings look brand new and defeat the whole point of the baseline diff.
    """
    if not text:
        return set()
    out: set[str] = set()
    if signal == "tests":
        for match in _PYTEST_FAILED.finditer(text):
            nodeid = match.group(1).strip()
            if nodeid:
                out.add(nodeid)
        return out
    for match in _LINTER_LINE.finditer(text):
        path = match.group("path").strip()
        rest = match.group("rest").strip()
        parsed = _LINT_CODE.match(rest)
        if parsed:
            code = (parsed.group("code") or "").strip()
            msg = (parsed.group("msg") or "").strip()
        else:  # pragma: no cover - _LINT_CODE matches anything
            code, msg = "", rest
        # Normalize away quoted identifiers so a rename does not read as new.
        msg = re.sub(r'"[^"]*"', '""', msg)
        msg = re.sub(r"'[^']*'", "''", msg)
        out.add(f"{path}|{code}|{msg[:120]}")
    return out


# ---------------------------------------------------------------------------
# Signal execution
# ---------------------------------------------------------------------------


def run_signal(
    signal: str,
    *,
    command: str,
    timeout_seconds: float,
    required: bool,
    cwd: str | None,
) -> SignalOutcome:
    """Run one signal and classify the result. Never raises."""
    if not command:
        if required:
            return SignalOutcome(
                name=signal,
                passed=False,
                unavailable=True,
                error="required verification command is unavailable",
            )
        return SignalOutcome(name=signal, passed=True, skipped=True, unavailable=True)
    try:
        args = shlex.split(command)
        if not args:
            raise ValueError("verification command is empty")
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            cwd=cwd or None,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return SignalOutcome(
            name=signal, passed=False, timed_out=True, timeout_seconds=timeout_seconds
        )
    except Exception as exc:
        return SignalOutcome(name=signal, passed=False, error=str(exc))
    combined = f"{proc.stdout}\n{proc.stderr}"
    passed = proc.returncode == 0
    return SignalOutcome(
        name=signal,
        passed=passed,
        returncode=proc.returncode,
        command=args,
        failures=set() if passed else extract_failures(signal, combined),
        output="" if passed else (proc.stderr or proc.stdout),
    )


# ---------------------------------------------------------------------------
# Baseline (merge base) support
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, cwd=cwd, timeout=_GIT_TIMEOUT
        )
    except Exception:
        log.debug("verify: git %s failed", " ".join(args), exc_info=True)
        return None


def resolve_baseline_ref(project_root: str) -> str | None:
    """Resolve the merge base to grade against.

    Prefers the upstream tracking branch, then a local default branch, then the
    previous commit. Returns None outside a git repo or on a root commit — the gate
    then reports plain pass/fail with ``baseline_used=False`` rather than pretending.
    """
    if not project_root:
        return None
    inside = _git(["rev-parse", "--is-inside-work-tree"], project_root)
    if inside is None or inside.returncode != 0 or inside.stdout.strip() != "true":
        return None
    for candidate in ("@{upstream}", "origin/HEAD", "origin/main", "main", "master"):
        merge_base = _git(["merge-base", "HEAD", candidate], project_root)
        if merge_base is not None and merge_base.returncode == 0:
            ref = merge_base.stdout.strip()
            if ref:
                head = _git(["rev-parse", "HEAD"], project_root)
                if head is not None and head.stdout.strip() == ref:
                    # Merge base IS HEAD (nothing committed on top): the previous
                    # commit is the only meaningful baseline.
                    parent = _git(["rev-parse", "HEAD~1"], project_root)
                    if parent is not None and parent.returncode == 0:
                        return parent.stdout.strip() or None
                    return None
                return ref
    parent = _git(["rev-parse", "HEAD~1"], project_root)
    if parent is not None and parent.returncode == 0:
        return parent.stdout.strip() or None
    return None


def run_baseline_signals(
    signals: dict[str, tuple[str, float]],
    *,
    project_root: str,
    ref: str,
) -> dict[str, set[str]]:
    """Run the given signals at ``ref`` in a throwaway detached worktree.

    ``signals`` maps name -> (command, timeout). Returns name -> failure set;
    signals that could not be run are simply absent, which the caller treats as
    "no baseline for this signal" rather than "baseline was clean".
    """
    out: dict[str, set[str]] = {}
    if not signals or not ref:
        return out
    tmpdir: str | None = None
    try:
        tmpdir = tempfile.mkdtemp(prefix="threnody-verify-baseline-")
        added = _git(["worktree", "add", "--detach", tmpdir, ref], project_root)
        if added is None or added.returncode != 0:
            log.debug(
                "verify: baseline worktree add failed: %s",
                added.stderr.strip() if added else "no result",
            )
            return out
        for name, (command, timeout) in signals.items():
            outcome = run_signal(
                name,
                command=command,
                timeout_seconds=timeout * BASELINE_TIMEOUT_MULTIPLIER,
                required=False,
                cwd=tmpdir,
            )
            if outcome.timed_out or outcome.error or outcome.unavailable:
                continue
            out[name] = outcome.failures
    except Exception:  # pragma: no cover - best-effort
        log.debug("verify: baseline run failed", exc_info=True)
    finally:
        if tmpdir:
            _git(["worktree", "remove", "--force", tmpdir], project_root)
            shutil.rmtree(tmpdir, ignore_errors=True)
    return out


# ---------------------------------------------------------------------------
# Baseline cache (per run)
# ---------------------------------------------------------------------------


def _baseline_cache_path(run_id: str) -> Path | None:
    try:
        from .run_log import run_log_dir

        return run_log_dir(run_id, create=True) / "verify_baseline.json"
    except Exception:  # pragma: no cover - best-effort
        log.debug("verify: baseline cache path unavailable", exc_info=True)
        return None


def load_baseline_cache(run_id: str, ref: str) -> dict[str, set[str]] | None:
    """Read this run's cached baseline, if it was taken at the same ref."""
    path = _baseline_cache_path(run_id) if run_id else None
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("ref") != ref:
            return None
        return {k: set(v) for k, v in (payload.get("signals") or {}).items()}
    except Exception:
        log.debug("verify: baseline cache read failed", exc_info=True)
        return None


def store_baseline_cache(run_id: str, ref: str, signals: dict[str, set[str]]) -> None:
    """Cache the baseline so later waves in the same run reuse it."""
    path = _baseline_cache_path(run_id) if run_id else None
    if path is None:
        return
    try:
        path.write_text(
            json.dumps({
                "ref": ref,
                "ts": time.time(),
                "signals": {k: sorted(v) for k, v in signals.items()},
            }),
            encoding="utf-8",
        )
    except Exception:
        log.debug("verify: baseline cache write failed", exc_info=True)


# ---------------------------------------------------------------------------
# Top-level gate
# ---------------------------------------------------------------------------


def run_verify_gate(
    gate_cfg: "VerifyGateConfig",
    *,
    project_root: str,
    baseline: bool = True,
    run_id: str | None = None,
    command_resolver: "Callable[[str, str], str] | None" = None,
) -> VerifyReport:
    """Run every configured signal and grade it against the merge base.

    A required signal that fails only because it was *already* failing at the
    baseline does not reject the run: it is reported under
    ``preexisting_failures`` and the signal is marked ``preexisting``. Only
    ``new_failures`` can produce ``warn``/``rejected``.

    ``command_resolver`` overrides ``auto`` command detection, preserving the
    orchestrator's existing extension point (and letting tests inject commands).
    """
    report = VerifyReport()
    resolved: dict[str, SignalOutcome] = {}
    commands: dict[str, tuple[str, float]] = {}
    resolve = command_resolver or detect_gate_command

    for name, sig_cfg in gate_cfg.signals.items():
        command = sig_cfg.command
        if command == "auto":
            command = resolve(name, project_root)
        timeout = float(getattr(sig_cfg, "timeout_seconds", 120) or 120)
        commands[name] = (command, timeout)
        resolved[name] = run_signal(
            name,
            command=command,
            timeout_seconds=timeout,
            required=bool(sig_cfg.required),
            cwd=project_root or None,
        )

    failing = {
        name: outcome
        for name, outcome in resolved.items()
        if not outcome.passed and not outcome.unavailable and not outcome.timed_out
    }

    baseline_failures: dict[str, set[str]] = {}
    if baseline and failing:
        ref = resolve_baseline_ref(project_root)
        report.baseline_ref = ref
        if ref:
            cached = load_baseline_cache(run_id or "", ref)
            if cached is not None:
                baseline_failures = cached
            else:
                baseline_failures = run_baseline_signals(
                    {n: commands[n] for n in failing},
                    project_root=project_root,
                    ref=ref,
                )
                if baseline_failures and run_id:
                    store_baseline_cache(run_id, ref, baseline_failures)
            report.baseline_used = bool(baseline_failures)
        else:
            report.note = "no merge base available — graded on plain pass/fail"

    any_required_new = False
    for name, outcome in resolved.items():
        entry = outcome.to_dict()
        if name in failing and name in baseline_failures:
            base = baseline_failures[name]
            new = outcome.failures - base
            preexisting = outcome.failures & base
            entry["baseline_compared"] = True
            entry["new_failures"] = sorted(new)[:50]
            entry["preexisting_failures"] = sorted(preexisting)[:50]
            report.new_failures.extend(f"{name}:{f}" for f in sorted(new))
            report.preexisting_failures.extend(f"{name}:{f}" for f in sorted(preexisting))
            if not new:
                # Failing, but nothing the run introduced.
                entry["preexisting"] = True
                entry["passed"] = True
            elif gate_cfg.signals[name].required:
                any_required_new = True
        elif name in failing:
            # No usable baseline: fall back to historical plain pass/fail.
            if outcome.failures:
                report.new_failures.extend(f"{name}:{f}" for f in sorted(outcome.failures))
            else:
                report.new_failures.append(f"{name}:failed")
            if gate_cfg.signals[name].required:
                any_required_new = True
        elif not outcome.passed and gate_cfg.signals[name].required:
            # Unavailable or timed-out required signal still blocks.
            any_required_new = True
        report.signals[name] = entry

    if any_required_new:
        report.verdict = VERDICT_REJECTED if gate_cfg.mode == "block" else VERDICT_WARN
    else:
        report.verdict = VERDICT_PASS
    return report


__all__ = [
    "VERDICT_PASS",
    "VERDICT_WARN",
    "VERDICT_REJECTED",
    "SignalOutcome",
    "VerifyReport",
    "detect_gate_command",
    "extract_failures",
    "run_signal",
    "resolve_baseline_ref",
    "run_baseline_signals",
    "load_baseline_cache",
    "store_baseline_cache",
    "run_verify_gate",
]
