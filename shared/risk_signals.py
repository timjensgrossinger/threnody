"""Single owner of the security-risk vocabulary used by routing and review fanout.

Two consumers used to keep hand-copied halves of one vocabulary:

* ``shared/config.py::DEFAULT_RISK_FILENAME_PATTERNS`` — 9 words, applied by
  :class:`shared.router.TaskRouter` as a tier floor over the *task text*.
* ``shared/review_fanout.py::_RISK_SIGNALS`` — ~30 tokens plus code patterns,
  applied over *file content* when tiering review cells.

The config half carried a comment instructing the reader to keep the two in sync.
They were not in sync and could not be: ``injection``, ``sanitiz``, ``redact``,
``untrusted``, ``privacy`` and ``pii`` were in neither, while ``oauth`` and
``saml`` were in the config half only — so a prompt-injection hardening task
matched nothing and routed to the cheapest tier. Both halves are now *derived*
from :data:`VOCABULARY` here, which makes the drift impossible instead of
merely discouraged.

The module deliberately imports nothing from :mod:`shared` at import time so
``config`` (the base of the package's import graph) can depend on it. File reads
and AST scans are lazy for the same reason.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from shared.db import Database

log = logging.getLogger(__name__)

# Source extensions worth scanning for risk. Non-code files carry no exploitable
# construct, so scanning them only adds false positives.
RISKY_EXTENSIONS = frozenset(
    {".py", ".js", ".ts", ".go", ".rb", ".java", ".php", ".cs", ".cpp", ".c"}
)


class RiskToken(NamedTuple):
    """One entry in the shared risk vocabulary.

    ``plain`` is the bare token, used where a substring/prefix match over a
    filename or free prose is wanted. ``fragment`` is the regex form used in the
    content-scan alternation — usually ``re.escape(plain)``, but several tokens
    need a suffix group (``encrypt(?:ion)?``) or literal punctuation
    (``os\\.system``).

    ``word_bounded`` is False only for the handful of fragments that carry their
    own anchors because they must end in an open paren rather than a word
    boundary (``exec(``, ``eval(``, ``yaml.load(``).

    ``concrete`` marks the subset that names an actually-dangerous construct
    rather than a risky *topic* — the difference between "this file is about
    authentication" and "this file interpolates into SQL".

    ``filename_safe`` marks tokens meaningful in a *filename*: ``billing.py`` is
    worth a floor. Tokens are excluded when they collide with unrelated words in
    that looser context (``card`` inside "cardinality", ``sql`` inside every
    ``sqlite_*`` helper in this repo, ``escape`` inside ordinary lexer and
    template code). Those stay in the content scan, where surrounding code makes
    them meaningful.

    ``prose_safe`` is the narrower set matchable against a free-form task
    description, and it is deliberately not the same set. The two are different
    jobs that one list used to do: ``billing.py`` names a payment surface, but
    "find files related to billing logic" is a read-only question that carries no
    risk at all — flooring it wasted a tier. Only tokens whose presence in a
    sentence genuinely implies security-sensitive change work qualify, which is
    also why the original hand-written 9-word list was tighter here than a
    filename list should be.
    """

    plain: str
    fragment: str
    concrete: bool = False
    filename_safe: bool = False
    prose_safe: bool = False
    word_bounded: bool = True


def _t(
    plain: str,
    fragment: str | None = None,
    *,
    concrete: bool = False,
    filename_safe: bool = False,
    prose_safe: bool | None = None,
    word_bounded: bool = True,
) -> RiskToken:
    """Build a vocabulary entry. ``prose_safe`` defaults to ``filename_safe``."""
    return RiskToken(
        plain=plain,
        fragment=fragment if fragment is not None else re.escape(plain),
        concrete=concrete,
        filename_safe=filename_safe,
        prose_safe=filename_safe if prose_safe is None else prose_safe,
        word_bounded=word_bounded,
    )


# The vocabulary. Order is preserved into the compiled alternations so the
# generated patterns stay diffable against the originals they replace.
VOCABULARY: tuple[RiskToken, ...] = (
    # --- risky topics: the work is security-relevant -----------------------
    _t("sql"),
    _t("subprocess", filename_safe=True, prose_safe=False),
    _t("auth", r"auth(?:enticate|entication|orization)?", filename_safe=True),
    _t("oauth", filename_safe=True),
    _t("saml", filename_safe=True),
    _t("jwt", filename_safe=True),
    _t("crypto", filename_safe=True),
    _t("cryptograph", r"cryptograph(?:y|ic)", filename_safe=True),
    _t("encrypt", r"encrypt(?:ion)?", filename_safe=True),
    _t("decrypt", r"decrypt(?:ion)?", filename_safe=True),
    _t("payment", filename_safe=True, prose_safe=False),
    _t("billing", filename_safe=True, prose_safe=False),
    _t("card"),
    _t("password", filename_safe=True),
    _t("secret", filename_safe=True),
    _t("credential", filename_safe=True),
    _t("keychain", filename_safe=True),
    _t("token", filename_safe=True),
    _t("api_key", r"api[_ -]?key", filename_safe=True),
    # --- untrusted-input surface: absent entirely before ------------------
    _t("injection", filename_safe=True),
    _t("prompt injection", r"prompt injection"),
    _t("sanitiz", r"sanitiz(?:e|es|er|ing|ation)?", filename_safe=True),
    _t("redact", r"redact(?:ion|ed)?", filename_safe=True),
    _t("untrusted", filename_safe=True),
    _t("privacy", filename_safe=True),
    _t("pii", filename_safe=True),
    _t("xss", filename_safe=True),
    _t("csrf", filename_safe=True),
    _t("escape", r"escap(?:e|es|ed|ing)"),
    _t("blocklist", filename_safe=True),
    _t("allowlist", filename_safe=True),
    # --- concrete dangerous constructs ------------------------------------
    _t("rce", concrete=True),
    _t("remote code execution", r"remote code execution", concrete=True),
    _t("os.system", r"os\.system", concrete=True),
    _t("cursor.execute", r"cursor\.execute", concrete=True),
    _t("raw_query", concrete=True),
    _t("shell=True", r"shell\s*=\s*True", concrete=True),
    _t("deserialize", r"deseriali[sz](?:e|ation)", concrete=True),
    _t("pickle.loads", r"pickle\.loads", concrete=True),
    _t("ssrf", concrete=True),
    _t("server-side request forgery", r"server-side request forgery", concrete=True),
    _t("path traversal", r"path traversal", concrete=True),
    _t("directory traversal", r"directory traversal", concrete=True),
    _t("exec(", r"\b(?:exec|eval)\s*\(", concrete=True, word_bounded=False),
    _t("yaml.load(", r"\byaml\.load\s*\(", concrete=True, word_bounded=False),
)


def _compile(tokens: "tuple[RiskToken, ...]") -> "re.Pattern[str]":
    """Compose one alternation from a token subset.

    Word-bounded fragments share a single ``\\b(?:...)\\b`` group; the rest carry
    their own anchors and are appended as top-level alternatives.
    """
    bounded = [tok.fragment for tok in tokens if tok.word_bounded]
    unbounded = [tok.fragment for tok in tokens if not tok.word_bounded]
    parts: list[str] = []
    if bounded:
        parts.append(r"\b(?:" + "|".join(bounded) + r")\b")
    parts.extend(unbounded)
    return re.compile("(?:" + "|".join(parts) + ")", re.IGNORECASE)


#: Any risk token — the file or task is security-relevant.
RISK_SIGNALS: "re.Pattern[str]" = _compile(VOCABULARY)

#: The concrete subset — a named dangerous construct, not merely a risky topic.
CONCRETE_HIGH_RISK_SIGNALS: "re.Pattern[str]" = _compile(
    tuple(tok for tok in VOCABULARY if tok.concrete)
)

#: Tokens matchable against a filename. Consumed by
#: ``config.DEFAULT_RISK_FILENAME_PATTERNS`` — adding a ``filename_safe`` token
#: above extends the host fanout's filename floor with no second edit.
FILENAME_RISK_TOKENS: tuple[str, ...] = tuple(
    tok.plain for tok in VOCABULARY if tok.filename_safe
)

#: The narrower subset matchable against a free-form task description. Consumed
#: by shared/router.py for its task-text floor. See ``RiskToken.prose_safe``.
PROSE_RISK_TOKENS: tuple[str, ...] = tuple(
    tok.plain for tok in VOCABULARY if tok.prose_safe
)


def has_risk_signals(content: str) -> bool:
    """True when ``content`` mentions any risk token."""
    return bool(content) and bool(RISK_SIGNALS.search(content))


def has_concrete_high_risk_signals(content: str) -> bool:
    """True when ``content`` names a concrete dangerous construct."""
    return bool(content) and bool(CONCRETE_HIGH_RISK_SIGNALS.search(content))


def compile_risk_floor_re(patterns: "list[str] | tuple[str, ...]") -> "re.Pattern[str] | None":
    """Compile a filename/prose vocabulary into a word-start matcher.

    Each pattern matches at a word boundary with optional trailing word chars, so
    ``auth`` catches ``authentication``/``authorization`` and ``credential``
    catches ``credentials``. Errs toward over-matching: flooring an occasional
    benign token to a higher tier is the safe direction. Returns None when the
    list is empty.

    Lives here rather than in :mod:`shared.router` because the operator-overridable
    pattern list it compiles is derived from :data:`VOCABULARY`, and the file
    evidence collector below needs the same matcher.
    """
    cleaned = [re.escape(p.strip()) for p in patterns if isinstance(p, str) and p.strip()]
    if not cleaned:
        return None
    return re.compile(r"\b(?:" + "|".join(cleaned) + r")\w*", re.IGNORECASE)


#: Default matcher over the vocabulary's filename-safe tokens.
DEFAULT_FILENAME_RISK_RE: "re.Pattern[str] | None" = compile_risk_floor_re(
    FILENAME_RISK_TOKENS
)

#: Default matcher over the prose-safe subset, for free-form task text.
DEFAULT_PROSE_RISK_RE: "re.Pattern[str] | None" = compile_risk_floor_re(
    PROSE_RISK_TOKENS
)

# Bounds on evidence collection. route_task is a hot, blocking call: a wide
# fan-out must not turn it into a whole-repo scan.
#
# Measured on this repo, at the cap, against its seven largest modules (the
# pathological case — 100KB+ each): 488ms cold, 3.9ms warm. code_intel.scan()
# caches by (path, content_sha) both in-process and in the `code_intel` table,
# so the AST cost is paid once per file revision and survives process restarts.
# A typical call resolves one to three ordinary files in well under 100ms.
# The count is capped rather than the size because code_intel already truncates
# at MAX_SCAN_BYTES, and a large file is exactly the one worth scanning.
MAX_EVIDENCE_FILES = 8


@dataclass(frozen=True)
class FileRisk:
    """Risk evidence for one resolved file."""

    path: str
    basename_risk: bool = False
    content_risk: bool = False
    #: High-severity smells from ``code_intel`` whose dimension is ``security``.
    security_smells: int = 0
    loc: int = 0


@dataclass(frozen=True)
class TaskRiskEvidence:
    """Aggregate risk evidence for the files a task will touch.

    Empty by default, and an empty instance must leave every consumer's
    behaviour exactly as it was before evidence existed — the router's other
    callers (planner, heuristic_plan, tests) pass nothing.
    """

    files: tuple[FileRisk, ...] = ()
    #: Paths named by the caller that could not be resolved or read.
    unresolved: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.files)

    @property
    def risk_files(self) -> int:
        """Files that are security-relevant by name, content, or detected defect.

        A detected defect counts even when no vocabulary token matched: the
        smell scan finds constructs the vocabulary cannot name, so a file can
        hold a confirmed high-severity smell with ``content_risk`` False.
        """
        return sum(
            1
            for f in self.files
            if f.basename_risk or f.content_risk or f.security_smells
        )

    @property
    def any_risk(self) -> bool:
        return self.risk_files > 0

    @property
    def any_basename_risk(self) -> bool:
        """True when a target's *filename* matches the risk vocabulary.

        The strong half of vocabulary evidence: ``credentials.py`` was named for
        what it holds. Content matching is the weak half — measured on this repo,
        the two highest-scoring files were ``risk_signals.py`` (95 hits, it
        defines the vocabulary) and ``code_intel.py`` (68, a static analyser),
        while ``quality_bias.py`` scored 1 hit and holds two real defects. So a
        content hit nudges the score and never sets a floor.
        """
        return any(f.basename_risk for f in self.files)

    @property
    def any_vocabulary_risk(self) -> bool:
        """True when a *name or content* token matched, ignoring detected defects.

        Separate from :attr:`any_risk` so the router can say which evidence
        actually fired: a file can hold a confirmed security smell while matching
        no vocabulary token, and reporting that as "file_content" would name the
        wrong detector.
        """
        return any(f.basename_risk or f.content_risk for f in self.files)

    @property
    def security_smells(self) -> int:
        """Total high-severity security-dimension smells across the file set."""
        return sum(f.security_smells for f in self.files)

    @property
    def any_security_smell(self) -> bool:
        return self.security_smells > 0

    @property
    def max_loc(self) -> int:
        return max((f.loc for f in self.files), default=0)


def _security_smell_count(path: str, content: str, db: "Database | None") -> int:
    """Count high-severity ``security``-dimension smells in ``content``.

    Deliberately narrower than "any high-severity smell". Measured on this repo:
    64 high-severity smells across 81 ``shared/*.py`` files, but 38 of those are
    ``silent_except`` on the ``edge`` dimension — a code-quality signal present in
    nearly half the codebase. Gating a high-tier floor on that would escalate
    almost every task here. Filtering to the ``security`` dimension leaves 8 files
    of 81, which is a signal rather than a constant.
    """
    try:
        from .code_intel import SEVERITY_HIGH, scan

        intel = scan(path, content=content, db=db)
    except Exception:
        log.debug("code_intel scan failed for %s", path, exc_info=True)
        return 0
    return sum(
        1
        for smell in intel.smells
        if smell.severity == SEVERITY_HIGH and smell.dimension == "security"
    )


def collect_task_evidence(
    paths: "list[str] | tuple[str, ...]",
    *,
    db: "Database | None" = None,
    filename_re: "re.Pattern[str] | None" = None,
    max_files: int = MAX_EVIDENCE_FILES,
) -> TaskRiskEvidence:
    """Gather risk evidence for already-resolved, in-repo file paths.

    Callers are responsible for containment: paths must have passed
    ``context.normalize_target_path`` / ``is_within_repo`` before arriving here.
    This function only reads and scans.

    Non-source extensions get a basename check but no content scan — a Markdown
    file has no exploitable construct, so scanning it only adds false positives.
    """
    if not paths:
        return TaskRiskEvidence()
    from pathlib import Path as _Path

    from .context import read_source_cached

    matcher = filename_re if filename_re is not None else DEFAULT_FILENAME_RISK_RE
    out: list[FileRisk] = []
    unresolved: list[str] = []
    for raw in list(paths)[:max_files]:
        path = str(raw)
        as_path = _Path(path)
        basename_risk = bool(matcher.search(as_path.name)) if matcher else False
        if as_path.suffix.lower() not in RISKY_EXTENSIONS:
            out.append(FileRisk(path=path, basename_risk=basename_risk))
            continue
        content: str | None = None
        try:
            content = read_source_cached(as_path, max_bytes=None)
        except Exception:
            log.debug("evidence read failed for %s", path, exc_info=True)
        # None means unreadable (missing, permission, decode failure); "" means a
        # real but empty file, which is a valid clean result and must not be
        # reported to the caller as an unresolved path.
        if content is None:
            unresolved.append(path)
            out.append(FileRisk(path=path, basename_risk=basename_risk))
            continue
        content_risk = has_risk_signals(content)
        # Scanned unconditionally, NOT gated on content_risk. The two detectors
        # answer different questions: the regex names a risky *topic*, while
        # code_intel detects an actual *defect*, and it finds ones the vocabulary
        # cannot name. `conn.execute(f"... {uid}")` is a confirmed high-severity
        # sql_interpolation smell but matches no risk token (the vocabulary has
        # `cursor.execute`, not `conn.execute`, and `\bsql\b` does not match
        # "sqlite3"). Gating the stronger signal behind the weaker one dropped
        # exactly the cases worth escalating for. The AST cost is bounded by
        # max_files and cached by (path, content_sha) in-process and in the
        # `code_intel` table, so it is paid once per file revision.
        smells = _security_smell_count(path, content, db)
        out.append(
            FileRisk(
                path=path,
                basename_risk=basename_risk,
                content_risk=content_risk,
                security_smells=smells,
                loc=sum(1 for line in content.splitlines() if line.strip()),
            )
        )
    return TaskRiskEvidence(files=tuple(out), unresolved=tuple(unresolved))
