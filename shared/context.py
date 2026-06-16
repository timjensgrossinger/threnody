"""
shared/context.py — Diff-based context injection (Phase 6).

Parses subtask descriptions for referenced file paths and function/class
names, reads only the relevant slices from disk, and injects a compact
source-code block into the agent prompt.  Agents get the exact code they
need; nothing more bloats their context window.

Public API
----------
    extract_references(text)           -> list[FileReference]
    read_file_context(ref, root)       -> str | None
    build_context_block(refs, root)    -> str
    enrich_subtask(subtask, root)      -> Subtask
    find_function_boundaries(lines, name) -> list[tuple[int, int]]
"""
from __future__ import annotations

import dataclasses
import hashlib
import logging
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .config import (
    ARTIFACT_MAX_INLINE_CHARS,
    CONTEXT_FUNCTION_RADIUS,
    CONTEXT_MAX_FILE_BYTES,
    CONTEXT_MAX_LINES_PER_FILE,
    CONTEXT_MAX_TOTAL_CHARS,
)

if TYPE_CHECKING:
    from .planner import Subtask

log = logging.getLogger(__name__)
ARTIFACT_INJECTION_SIZE_BUDGET = 1000

# --- Wave-scoped source cache ------------------------------------------------
# During a fan-out wave many subtasks reference the same source file; without a
# cache each enrichment re-reads it from disk (e.g. a review wave of N×dims
# cells on one file = N×dims reads). Keying on (st_mtime_ns, st_size) means any
# mid-wave write changes the key → a fresh read, so stale content is never
# served past a modification. Bounded LRU keeps RAM in check; a lock keeps the
# cache correct under the orchestrator's ThreadPoolExecutor waves.
_FILE_CACHE: "OrderedDict[str, tuple[int, int, str]]" = OrderedDict()
_FILE_CACHE_MAX = 256
_FILE_CACHE_LOCK = threading.Lock()


def read_source_cached(path: Path, *, max_bytes: int | None = CONTEXT_MAX_FILE_BYTES) -> str | None:
    """Return the full text of *path*, served from an mtime+size-keyed cache.

    Returns ``None`` if the file is unreadable or (when *max_bytes* is set)
    exceeds the byte cap. Pass ``max_bytes=None`` to bypass the cap (the cached
    bytes are identical either way — only the cap gate differs).
    """
    try:
        st = path.stat()
    except OSError:
        return None
    if max_bytes is not None and st.st_size > max_bytes:
        return None
    key = str(path)
    sig0, sig1 = st.st_mtime_ns, st.st_size
    with _FILE_CACHE_LOCK:
        hit = _FILE_CACHE.get(key)
        if hit is not None and hit[0] == sig0 and hit[1] == sig1:
            _FILE_CACHE.move_to_end(key)
            return hit[2]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return None
    with _FILE_CACHE_LOCK:
        _FILE_CACHE[key] = (sig0, sig1, text)
        _FILE_CACHE.move_to_end(key)
        while len(_FILE_CACHE) > _FILE_CACHE_MAX:
            _FILE_CACHE.popitem(last=False)
    return text


def clear_source_cache() -> None:
    """Drop all cached file contents (optional wave-boundary reset)."""
    with _FILE_CACHE_LOCK:
        _FILE_CACHE.clear()
SOURCE_BLOCK_HEADER = "\n--- RELEVANT SOURCE CODE ---\n"
SOURCE_BLOCK_FOOTER = "\n--- END SOURCE CODE ---\n"
ARTIFACT_BLOCK_HEADER = "\n--- ARTIFACT HANDOFF ---\n"
ARTIFACT_BLOCK_FOOTER = "\n--- END ARTIFACT HANDOFF ---\n"

# Sentinel prefix used by Layer 2 summary truncation — idempotency marker.
_TRUNCATION_SENTINEL_PREFIX = "[... "

# Comment-only line patterns per language for Layer 4 structural stripping.
_COMMENT_PATTERNS = [
    re.compile(r"^\s*#.*$"),             # Python
    re.compile(r"^\s*//.*$"),            # JS/TS/Go single-line
    re.compile(r"^\s*/\*.*\*/\s*$"),     # JS/TS inline block comment
]


# ---------------------------------------------------------------------------
# Plan 15 — Context Compression
# ---------------------------------------------------------------------------

@dataclass
class CompressedContext:
    """Result of ContextCompressor.compress()."""
    text: str
    original_len: int
    compressed_len: int
    layers_applied: list[str] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        if self.original_len == 0:
            return 0.0
        return 1.0 - (self.compressed_len / self.original_len)


class ContextCompressor:
    """Multi-layer context compressor (plan 15).

    Layers applied in order, each togglable:
      file   → Layer 1 (diff-only) + Layer 4 (structural strip)
      output → Layer 2 (summary truncation) + Layer 3 (dedup)
      full   → all 4 layers

    The dedup hash map is instance-scoped — one ContextCompressor per
    Orchestrator run keeps dedup within a single task execution.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        layers: list[str] | None = None,
        max_context_chars: int = 8000,
        min_ratio_to_log: float = 0.5,
    ) -> None:
        self._enabled = enabled
        self._layers = set(layers or ["diff_only", "summary_truncation", "dedup", "structural_strip"])
        self._max_context_chars = max_context_chars
        self._min_ratio_to_log = min_ratio_to_log
        self._seen_hashes: dict[str, str] = {}  # hash → first-seen ref key

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compress(self, text: str, mode: str) -> CompressedContext:
        """Compress *text* according to *mode*.

        mode values:
          "file"   — Layer 1 (diff-only placeholder) + Layer 4 (structural strip)
          "output" — Layer 2 (summary truncation) + Layer 3 (dedup)
          "full"   — all 4 layers
        """
        if not self._enabled or not text:
            return CompressedContext(
                text=text, original_len=len(text), compressed_len=len(text)
            )

        original_len = len(text)
        layers_applied: list[str] = []
        result = text

        if mode in ("output", "full"):
            result, applied = self._layer2_truncate(result)
            layers_applied.extend(applied)
            result, applied = self._layer3_dedup(result)
            layers_applied.extend(applied)

        if mode in ("file", "full"):
            result, applied = self._layer4_strip(result)
            layers_applied.extend(applied)

        return CompressedContext(
            text=result,
            original_len=original_len,
            compressed_len=len(result),
            layers_applied=layers_applied,
        )

    # ------------------------------------------------------------------
    # Layer 2 — summary-first truncation
    # ------------------------------------------------------------------

    def _layer2_truncate(self, text: str) -> tuple[str, list[str]]:
        if "summary_truncation" not in self._layers:
            return text, []
        if len(text) <= self._max_context_chars:
            return text, []
        # Already compressed — idempotent.
        if text.startswith(_TRUNCATION_SENTINEL_PREFIX):
            return text, []
        keep_head = 400
        keep_tail = 200
        omitted = len(text) - keep_head - keep_tail
        sentinel = f"[... {omitted} chars omitted ...]"
        result = text[:keep_head] + "\n" + sentinel + "\n" + text[-keep_tail:]
        return result, ["summary_truncation"]

    # ------------------------------------------------------------------
    # Layer 3 — in-memory dedup by hash
    # ------------------------------------------------------------------

    def _layer3_dedup(self, text: str) -> tuple[str, list[str]]:
        if "dedup" not in self._layers:
            return text, []
        digest = hashlib.sha256(text.encode()).hexdigest()
        short = digest[:8]
        if short in self._seen_hashes:
            return f"[ref: {short}]", ["dedup"]
        self._seen_hashes[short] = text[:64]
        return text, []

    # ------------------------------------------------------------------
    # Layer 4 — structural stripping
    # ------------------------------------------------------------------

    def _layer4_strip(self, text: str) -> tuple[str, list[str]]:
        if "structural_strip" not in self._layers:
            return text, []
        lines = text.splitlines()
        out: list[str] = []
        stripped_any = False
        prev_import: str | None = None
        consecutive_import_repeats = 0
        for line in lines:
            stripped = line.rstrip()
            # Drop blank lines
            if not stripped:
                stripped_any = True
                continue
            # Drop comment-only lines
            if any(p.match(stripped) for p in _COMMENT_PATTERNS):
                stripped_any = True
                continue
            # Collapse repeated import blocks (>3 identical consecutive leading lines)
            if stripped.startswith(("import ", "from ")):
                if stripped == prev_import:
                    consecutive_import_repeats += 1
                    if consecutive_import_repeats > 3:
                        stripped_any = True
                        continue
                else:
                    prev_import = stripped
                    consecutive_import_repeats = 1
            else:
                prev_import = None
                consecutive_import_repeats = 0
            out.append(stripped)
        if not stripped_any:
            return text, []
        return "\n".join(out), ["structural_strip"]


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------

@dataclass
class FileReference:
    """A file path extracted from a subtask description, with optional hints."""
    path: str
    functions: list[str] = field(default_factory=list)
    line_ranges: list[tuple[int, int]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Regex patterns (compiled once at import time)
# ---------------------------------------------------------------------------

# File-path patterns — ordered most-specific first to avoid partial matches.
# False negatives preferred over false positives.
_BACKTICK_PATH = re.compile(
    r"`([A-Za-z0-9_./-][A-Za-z0-9_./ -]*?\.[A-Za-z][A-Za-z0-9]*)`"
)
_ABSOLUTE_PATH = re.compile(
    r"(?<!\w)(/(?:[A-Za-z0-9_.+-]+/)*[A-Za-z0-9_.+-]+\.[A-Za-z][A-Za-z0-9]*)"
)
_RELATIVE_PATH = re.compile(
    r"(?<!\w)(\.{1,2}/(?:[A-Za-z0-9_.+-]+/)*[A-Za-z0-9_.+-]+\.[A-Za-z][A-Za-z0-9]*)"
)
# Bare relative paths like `src/foo.py` or `shared/context.py`
# Must contain at least one slash so we don't grab plain words ending in .py.
_BARE_PATH = re.compile(
    r"(?<!\w)([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\.[A-Za-z][A-Za-z0-9]*)"
)

# Function / class reference patterns
_DEF_NAME = re.compile(r"\bdef\s+([A-Za-z_][A-Za-z0-9_]*)")
_CLASS_NAME = re.compile(r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)")
_CALL_NAME = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(\)")
_METHOD_NAME = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")

# Line-reference patterns  — `line 42`, `lines 10-20`, `L42`, `L10-L20`
_LINE_SINGLE = re.compile(r"(?:line\s+|L)(\d+)\b", re.IGNORECASE)
_LINE_RANGE = re.compile(
    r"(?:lines?\s+|L)(\d+)\s*[-–]\s*L?(\d+)\b", re.IGNORECASE
)

# Words that look like function calls but are noise
_NOISE_CALLS: frozenset[str] = frozenset({
    "if", "for", "while", "with", "assert", "return", "yield",
    "print", "len", "range", "type", "list", "dict", "set", "tuple",
    "str", "int", "float", "bool", "None", "True", "False",
    "super", "property", "staticmethod", "classmethod",
})


# ---------------------------------------------------------------------------
# Reference extraction
# ---------------------------------------------------------------------------

def extract_references(text: str) -> list[FileReference]:
    """Parse *text* for file paths and associated function/line hints.

    Associates functions and line numbers with the most recently mentioned
    file path.  Returns a deduplicated list of :class:`FileReference` objects.
    If no file paths are found, returns an empty list.
    """
    # ---- 1. Collect all file-path spans (position → path string) ----------
    path_spans: list[tuple[int, str]] = []  # (start_pos, path)
    seen_paths: set[str] = set()

    for pattern in (_BACKTICK_PATH, _ABSOLUTE_PATH, _RELATIVE_PATH, _BARE_PATH):
        for m in pattern.finditer(text):
            p = m.group(1)
            if p not in seen_paths:
                seen_paths.add(p)
                path_spans.append((m.start(), p))

    if not path_spans:
        return []

    # Sort by position so "most recently mentioned" tracking is correct.
    path_spans.sort(key=lambda x: x[0])
    path_positions = [pos for pos, _ in path_spans]

    # Build a mutable dict: path → FileReference (preserving insertion order)
    refs: dict[str, FileReference] = {p: FileReference(path=p) for _, p in path_spans}

    def _closest_path_before(pos: int) -> str | None:
        """Return the path string closest to *pos*, preferring one that precedes it.

        If no path appears before *pos*, fall back to the nearest path after it
        (common in phrases like "fix def foo in src/bar.py").  When there is
        exactly one file path in the text, it always wins regardless of position.
        """
        if len(path_spans) == 1:
            return path_spans[0][1]

        # Prefer the last path whose start is ≤ pos.
        before_idx: int | None = None
        for i, p_pos in enumerate(path_positions):
            if p_pos <= pos:
                before_idx = i
        if before_idx is not None:
            return path_spans[before_idx][1]

        # Fall back to the nearest path after pos.
        for i, p_pos in enumerate(path_positions):
            if p_pos > pos:
                return path_spans[i][1]
        return None

    # ---- 2. Associate function names with preceding file paths -------------
    fn_names: set[str] = set()
    for pattern in (_DEF_NAME, _CLASS_NAME):
        for m in pattern.finditer(text):
            name = m.group(1)
            fn_names.add(name)
            host = _closest_path_before(m.start())
            if host and name not in refs[host].functions:
                refs[host].functions.append(name)

    for pattern in (_CALL_NAME, _METHOD_NAME):
        for m in pattern.finditer(text):
            name = m.group(1)
            if name in _NOISE_CALLS or name in fn_names:
                continue
            host = _closest_path_before(m.start())
            if host and name not in refs[host].functions:
                refs[host].functions.append(name)

    # ---- 3. Associate line references with preceding file paths ------------
    # Ranges must be checked before singles to avoid partial matches.
    for m in _LINE_RANGE.finditer(text):
        start, end = int(m.group(1)), int(m.group(2))
        if start > end:
            start, end = end, start
        host = _closest_path_before(m.start())
        if host:
            pair: tuple[int, int] = (start, end)
            if pair not in refs[host].line_ranges:
                refs[host].line_ranges.append(pair)

    # Mask range matches so single-line pattern doesn't re-grab them
    masked = _LINE_RANGE.sub(lambda m: " " * len(m.group(0)), text)
    for m in _LINE_SINGLE.finditer(masked):
        lineno = int(m.group(1))
        host = _closest_path_before(m.start())
        if host:
            pair = (lineno, lineno)
            if pair not in refs[host].line_ranges:
                refs[host].line_ranges.append(pair)

    return list(refs.values())


# ---------------------------------------------------------------------------
# Function boundary detection
# ---------------------------------------------------------------------------

def find_function_boundaries(
    lines: list[str], func_name: str
) -> list[tuple[int, int]]:
    """Return (start, end) 0-indexed line pairs for every definition of *func_name*.

    Searches for ``def func_name`` or ``class func_name``.  Block end is
    determined by the next line at the same-or-lower indentation level (or
    EOF).  Multiple definitions (e.g. same name in different classes) are all
    returned.
    """
    header_re = re.compile(
        r"^([ \t]*)(?:def|class)\s+" + re.escape(func_name) + r"\b"
    )
    results: list[tuple[int, int]] = []

    for i, line in enumerate(lines):
        m = header_re.match(line)
        if not m:
            continue
        def_indent = len(m.group(1).expandtabs())
        end = len(lines) - 1  # default: reaches EOF

        for j in range(i + 1, len(lines)):
            stripped = lines[j].rstrip()
            if not stripped:
                continue  # blank / whitespace-only lines don't end a block
            expanded = lines[j].expandtabs()
            expanded_stripped = expanded.rstrip()
            line_indent = len(expanded_stripped) - len(expanded_stripped.lstrip())
            if line_indent <= def_indent and not expanded_stripped.lstrip().startswith("#"):
                # Walk back past any trailing blank lines to get the true end.
                end = j - 1
                while end > i and not lines[end].rstrip():
                    end -= 1
                break

        results.append((i, end))

    return results


# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------

def read_file_context(
    ref: FileReference,
    project_root: str | None = None,
) -> str | None:
    """Read the relevant portion of the file described by *ref*.

    Resolution order:
    1. ``ref.path`` as-is (absolute paths resolve immediately).
    2. ``ref.path`` relative to *project_root* (if given).
    3. ``ref.path`` relative to cwd.

    Returns a string with a header comment, or ``None`` if the file cannot
    be read.
    """
    path = _resolve_path(ref.path, project_root)
    if path is None:
        log.debug("context: could not resolve %r (root=%s)", ref.path, project_root)
        return None

    raw = read_source_cached(path, max_bytes=CONTEXT_MAX_FILE_BYTES)
    if raw is None:
        log.debug("context: cannot read %s (unreadable or exceeds %d byte cap)", path, CONTEXT_MAX_FILE_BYTES)
        return None

    all_lines = raw.splitlines()
    if not all_lines:
        log.debug("context: skipping empty file %s", path)
        return None

    # ---- Determine which lines to extract ----------------------------------
    if ref.functions:
        segments = _lines_for_functions(all_lines, ref.functions)
    elif ref.line_ranges:
        segments = _lines_for_ranges(all_lines, ref.line_ranges)
    else:
        segments = [(0, min(len(all_lines), CONTEXT_MAX_LINES_PER_FILE) - 1)]

    if not segments:
        log.debug("context: no matching segments in %s", path)
        return None

    return _assemble_content(ref.path, all_lines, segments)


_SENSITIVE_PATH_SEGMENTS = frozenset({
    ".ssh", ".gnupg", ".aws", ".kube", ".docker",
    ".npmrc", ".pypirc", ".netrc",
})


def normalize_target_path(raw: str, repo_root: str | Path) -> Path:
    """Resolve a write target relative to *repo_root*.

    Relative paths are interpreted under *repo_root*. Paths that use parent
    traversal (``..``) or point into sensitive directories raise ValueError.
    Absolute paths outside *repo_root* are allowed to resolve so callers can
    route them through an approval flow.
    """
    if not raw or not raw.strip():
        raise ValueError("Target path must be a non-empty string")

    repo_base = Path(repo_root).expanduser().resolve()
    raw_path = Path(raw).expanduser()

    if ".." in raw_path.parts:
        raise ValueError(f"Target path uses parent traversal: {raw}")
    if _SENSITIVE_PATH_SEGMENTS & set(raw_path.parts):
        raise ValueError(f"Target path points into a sensitive location: {raw}")

    candidate = raw_path if raw_path.is_absolute() else repo_base / raw_path
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise ValueError(f"Could not resolve target path {raw!r}: {exc}") from exc

    if _SENSITIVE_PATH_SEGMENTS & set(resolved.parts):
        raise ValueError(f"Target path points into a sensitive location: {resolved}")
    return resolved


def is_within_repo(path: str | Path, repo_root: str | Path) -> bool:
    """Return True when *path* resolves under *repo_root*."""
    repo_base = Path(repo_root).expanduser().resolve()
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
        return resolved.is_relative_to(repo_base)
    except (OSError, ValueError):
        return False


def _resolve_path(raw: str, project_root: str | None) -> Path | None:
    """Try to find *raw* on disk; return resolved Path or None.

    CWE-22 guard: resolved paths must fall within *project_root* (or cwd when
    no root is given).  Paths that escape the trusted base or point into known
    sensitive directories are blocked.
    """
    bases: list[Path] = []
    if project_root:
        bases.append(Path(project_root).resolve())
    bases.append(Path.cwd().resolve())

    candidates: list[Path] = [Path(raw)]
    if project_root:
        candidates.append(Path(project_root) / raw)
    candidates.append(Path.cwd() / raw)

    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
            if not resolved.is_file():
                continue
            if not any(resolved.is_relative_to(base) for base in bases):
                log.warning(
                    "context: blocked path traversal — %r resolves to %s "
                    "(outside project root / cwd)", raw, resolved,
                )
                continue
            if _SENSITIVE_PATH_SEGMENTS & set(resolved.parts):
                log.warning("context: blocked sensitive path %s", resolved)
                continue
            return resolved
        except (OSError, ValueError):
            continue
    return None


def _lines_for_functions(
    all_lines: list[str], func_names: list[str]
) -> list[tuple[int, int]]:
    """Collect line ranges for each requested function, expanded by CONTEXT_FUNCTION_RADIUS."""
    n = len(all_lines)
    raw_segments: list[tuple[int, int]] = []
    for name in func_names:
        for start, end in find_function_boundaries(all_lines, name):
            lo = max(0, start - CONTEXT_FUNCTION_RADIUS)
            hi = min(n - 1, end + CONTEXT_FUNCTION_RADIUS)
            raw_segments.append((lo, hi))
    return _merge_segments(raw_segments)


def _lines_for_ranges(
    all_lines: list[str], line_ranges: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Convert 1-indexed line ranges to 0-indexed, clamped to file length."""
    n = len(all_lines)
    raw: list[tuple[int, int]] = []
    for start1, end1 in line_ranges:
        lo = max(0, start1 - 1)
        hi = min(n - 1, end1 - 1)
        if lo <= hi:
            raw.append((lo, hi))
    return _merge_segments(raw)


def _merge_segments(segments: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping/adjacent (start, end) pairs (0-indexed)."""
    if not segments:
        return []
    sorted_segs = sorted(segments)
    merged: list[tuple[int, int]] = [sorted_segs[0]]
    for start, end in sorted_segs[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + 1:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _assemble_content(
    label: str,
    all_lines: list[str],
    segments: list[tuple[int, int]],
) -> str:
    """Build the annotated content string from selected segments."""
    parts: list[str] = []
    total_lines = 0

    for lo, hi in segments:
        # Enforce per-file line cap across all segments combined.
        available = CONTEXT_MAX_LINES_PER_FILE - total_lines
        if available <= 0:
            break
        hi = min(hi, lo + available - 1)
        chunk = all_lines[lo : hi + 1]
        # 1-indexed display
        header = f"# --- {label} (lines {lo + 1}-{hi + 1}) ---"
        parts.append(header)
        parts.extend(chunk)
        total_lines += hi - lo + 1

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Context block assembly
# ---------------------------------------------------------------------------

def build_context_block(
    refs: list[FileReference],
    project_root: str | None = None,
    max_total_chars: int = CONTEXT_MAX_TOTAL_CHARS,
    compressor: ContextCompressor | None = None,
) -> str:
    """Concatenate file contexts for all references into a single block.

    Truncates the combined output to ``max_total_chars`` characters.
    Returns an empty string if nothing could be read.
    """
    content_budget = max_total_chars - len(SOURCE_BLOCK_HEADER) - len(SOURCE_BLOCK_FOOTER)
    if content_budget <= 0:
        return ""

    pieces: list[str] = []
    total_chars = 0

    for ref in refs:
        separator_len = 0 if not pieces else 2
        available = content_budget - total_chars - separator_len
        if available <= 0:
            log.debug("context: hit char cap, skipping %s", ref.path)
            break
        content = read_file_context(ref, project_root)
        if content is None:
            continue
        if compressor is not None:
            content = compressor.compress(content, "file").text
        if len(content) > available:
            content = content[:available]
            log.debug("context: truncated %s to fit char cap", ref.path)
        pieces.append(content)
        total_chars += separator_len + len(content)

    if not pieces:
        return ""

    body = "\n\n".join(pieces)
    return f"{SOURCE_BLOCK_HEADER}{body}{SOURCE_BLOCK_FOOTER}"


def compact_summary_truncate(
    summary: str,
    max_chars: int = ARTIFACT_INJECTION_SIZE_BUDGET,
) -> str:
    """Deterministically cap one injected summary and mark truncation."""
    cap = max(0, min(max_chars, ARTIFACT_MAX_INLINE_CHARS))
    if len(summary) <= cap:
        return summary
    marker = " [truncated]"
    if cap <= len(marker):
        return marker[:cap]
    return summary[: cap - len(marker)].rstrip() + marker


def make_compact_summary(payload: str) -> dict[str, object]:
    """Create the persisted summary envelope for one artifact payload."""
    summary_text = compact_summary_truncate(payload, max_chars=ARTIFACT_MAX_INLINE_CHARS)
    return {
        "summary_text": summary_text,
        "length_chars": len(summary_text),
    }


def make_artifact_envelope(
    artifact_type: str,
    compact_summary: dict[str, object],
    *,
    producer_subtask_id: str | None = None,
    parent_execution_id: str | None = None,
) -> dict[str, object]:
    """Build one child-visible artifact envelope, including hierarchical metadata."""
    summary_text = str(compact_summary.get("summary_text", ""))
    raw_length = compact_summary.get("length_chars", len(summary_text))
    envelope = {
        "artifact_type": artifact_type,
        "summary_text": summary_text,
        "length_chars": int(raw_length) if isinstance(raw_length, int) else len(summary_text),
        "artifact_ref": str(compact_summary.get("artifact_ref", "")),
        "producer_subtask_id": producer_subtask_id,
        "parent_execution_id": parent_execution_id,
    }
    return envelope


def build_artifact_context_block(
    artifacts: list[dict[str, object]],
    max_total_chars: int = CONTEXT_MAX_TOTAL_CHARS,
    compressor: ContextCompressor | None = None,
) -> str:
    """Build a compact artifact handoff block within the shared context budget."""
    content_budget = max_total_chars - len(ARTIFACT_BLOCK_HEADER) - len(ARTIFACT_BLOCK_FOOTER)
    if not artifacts or content_budget <= 0:
        return ""

    sections: list[str] = []
    total_chars = 0
    omitted = 0
    for index, artifact in enumerate(artifacts):
        artifact_type = str(artifact.get("artifact_type", "artifact"))
        artifact_ref = str(artifact.get("artifact_ref", ""))
        raw_summary_text = str(artifact.get("summary_text", ""))
        if compressor is not None:
            raw_summary_text = compressor.compress(raw_summary_text, "output").text
        prefix = (
            f"Artifact type: {artifact_type}\n"
            f"Reference: {artifact_ref}\n"
            "Summary: "
        )
        remaining_after = len(artifacts) - index - 1
        omission_reserve = (
            len(f"\n\n[omitted {1 + remaining_after} artifact(s) due to context budget]")
            if remaining_after > 0
            else 0
        )
        separator_len = 0 if not sections else 2
        available = content_budget - total_chars - separator_len - omission_reserve
        if available <= len(prefix):
            omitted += 1 + remaining_after
            break

        summary_budget = available - len(prefix)
        if len(raw_summary_text) > summary_budget and summary_budget <= len(" [truncated]"):
            omitted += 1 + remaining_after
            break

        summary_text = compact_summary_truncate(
            raw_summary_text,
            max_chars=summary_budget,
        )
        section = prefix + summary_text
        sections.append(section)
        total_chars += separator_len + len(section)

    if omitted:
        omitted_line = f"\n\n[omitted {omitted} artifact(s) due to context budget]"
        if total_chars + len(omitted_line) <= content_budget:
            sections.append(omitted_line.strip())

    if not sections:
        return ""

    body = "\n\n".join(sections)
    return f"{ARTIFACT_BLOCK_HEADER}{body}{ARTIFACT_BLOCK_FOOTER}"


def make_summary_for_wave(
    artifacts: list[dict[str, object]],
    max_total_chars: int = CONTEXT_MAX_TOTAL_CHARS,
) -> str:
    """Build a summary-only artifact handoff block for a completed wave."""
    return build_artifact_context_block(artifacts, max_total_chars=max_total_chars)


# ---------------------------------------------------------------------------
# Subtask enrichment
# ---------------------------------------------------------------------------

def enrich_subtask(
    subtask: "Subtask",
    project_root: str | None = None,
    *,
    db: object | None = None,
    execution_id: str | None = None,
    plan_revision: int | None = None,
    current_wave: int | None = None,
    prefetched_artifacts: list[dict[str, object]] | None = None,
) -> "Subtask":
    """Return a copy of *subtask* with agent context and relevant source code prepended to its description.

    Does **not** mutate the original subtask.  If no context can be built
    (no file references found, or all reads fail), returns the subtask with agent context if present.
    """
    # Prepend agent context if available (from Wave 3a agent matching)
    description = subtask.description
    if subtask.agent_context:
        description = f"{subtask.agent_context}\n\n{description}"

    artifact_block = ""
    artifact_count = 0
    artifacts: list[dict[str, object]] = []
    if subtask.consumes:
        if prefetched_artifacts is not None:
            if isinstance(prefetched_artifacts, list):
                requested_types = set(subtask.consumes)
                artifacts = [
                    artifact
                    for artifact in prefetched_artifacts
                    if isinstance(artifact, dict)
                    and str(artifact.get("artifact_type", "")) in requested_types
                ]
            else:
                log.warning(
                    "context: ignoring invalid prefetched artifacts for subtask %d",
                    subtask.id,
                )
        elif (
            db is not None
            and execution_id is not None
            and plan_revision is not None
            and hasattr(db, "get_artifacts_for_consumes")
        ):
            upto_wave = None if current_wave is None else max(0, current_wave - 1)
            try:
                fetched_artifacts = db.get_artifacts_for_consumes(
                    execution_id,
                    plan_revision,
                    list(subtask.consumes),
                    upto_wave=upto_wave,
                )
            except Exception:
                log.warning(
                    "context: failed to fetch artifacts for subtask %d",
                    subtask.id,
                    exc_info=True,
                )
                fetched_artifacts = []
            if isinstance(fetched_artifacts, list):
                artifacts = fetched_artifacts
                # Rule: increment consume counter in telemetry when artifacts are fetched
                try:
                    if db is not None and execution_id:
                        # write a minimal telemetry row to record consumes
                        try:
                            db.write_telemetry_row(
                                session_id=str(id(db)),
                                task_hash=execution_id,
                                agent_id=0,
                                tier="context",
                                model="",
                                artifact_consume_count=len(artifacts),
                            )
                        except Exception:
                            # best-effort; don't fail enrichment on telemetry errors
                            log.debug("context: failed to write artifact_consume telemetry", exc_info=True)
                except Exception:
                    pass
            elif fetched_artifacts is not None:
                log.warning(
                    "context: ignoring invalid artifact payload for subtask %d",
                    subtask.id,
                )
        artifact_count = len(artifacts)
        artifact_block = build_artifact_context_block(artifacts)
        if artifact_block:
            log.info(
                "context: injected %d artifact(s) into subtask %d (+%d chars)",
                artifact_count,
                subtask.id,
                len(artifact_block),
            )

    refs = extract_references(description)
    block = ""
    if refs:
        remaining_context = max(0, CONTEXT_MAX_TOTAL_CHARS - len(artifact_block))
        block = build_context_block(refs, project_root, max_total_chars=remaining_context)
        if not block:
            log.debug("context: could not read any referenced files for subtask %d", subtask.id)
    else:
        log.debug("context: no file references in subtask %d", subtask.id)

    if not artifact_block and not block:
        if subtask.agent_context:
            return dataclasses.replace(subtask, description=description)
        return subtask

    enriched_description = description + artifact_block + block
    log.info(
        "context: enriched subtask %d (+%d chars, %d file ref(s), %d artifact(s))",
        subtask.id,
        len(artifact_block) + len(block),
        len(refs),
        artifact_count,
    )
    return dataclasses.replace(subtask, description=enriched_description)
