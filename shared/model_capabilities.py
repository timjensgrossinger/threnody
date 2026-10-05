"""Per-model reasoning-effort capabilities from the models.dev community catalog.

A CLI's ``--effort``/``--variant`` flag is only meaningful when the *chosen model*
takes that effort: Copilot rejects or ignores ``--effort`` on a model without one
(``claude-haiku-4.5`` reasons with a token budget, ``gpt-5.4-nano`` has no knob at
all), and an OpenCode variant name is model specific. This module answers "which
effort levels does model X accept" without spending an LLM call.

Sources, freshest first:

* OpenCode's own copy of models.dev (``$XDG_CACHE_HOME/opencode/models.json``),
  which OpenCode refreshes itself;
* Threnody's copy under the install dir (``.runtime/models_dev.json``), written
  from ``https://models.dev/api.json`` at most once per process per TTL.

Both share the models.dev shape ``{provider: {"models": {id: {"reasoning": bool,
"reasoning_options": [{"type": "effort", "values": [...]}, ...]}}}}``. Offline
is safe: no network → a stale cache → else unknown (``None``), and callers treat
unknown conservatively (no flag).

Copilot's model capabilities are deliberately NOT read from
``api.githubcopilot.com/models``: that endpoint is undocumented and only answers
clients presenting VS Code integration headers, which a router would have to
spoof — a provider-terms risk (see docs/LEGAL.md).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MODELS_DEV_URL = "https://models.dev/api.json"
CACHE_TTL_SECONDS = 86_400
FETCH_TIMEOUT_SECONDS = 3.0
# `gh copilot -- --help`: --effort, --reasoning-effort <none|minimal|low|medium|high|xhigh|max>.
EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
COPILOT_CLI_EFFORT_LEVELS = frozenset(EFFORT_ORDER)

# provider -> model id -> accepted effort levels (empty = known, takes none).
_Index = dict[str, dict[str, frozenset[str]]]
_INDEX_CACHE: dict[str, tuple[float, _Index]] = {}
_FETCH_ATTEMPTED_AT = 0.0


def _opencode_cache_path() -> Path:
    override = os.environ.get("THRENODY_OPENCODE_MODELS_JSON", "").strip()
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CACHE_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".cache"
    return root / "opencode" / "models.json"


def _threnody_cache_path() -> Path:
    # Resolved at call time (like the journal root) so tests can redirect it.
    override = os.environ.get("THRENODY_MODELS_DEV_CACHE", "").strip()
    if override:
        return Path(override).expanduser()
    from .config import BASE_DIR

    return BASE_DIR / ".runtime" / "models_dev.json"


def _network_allowed() -> bool:
    from .env import env_truthy, test_mode_enabled

    return not (test_mode_enabled() or env_truthy("THRENODY_MODELS_DEV_OFFLINE"))


def _effort_levels(entry: dict[str, Any]) -> frozenset[str] | None:
    options = entry.get("reasoning_options")
    if isinstance(options, list):
        levels: set[str] = set()
        for option in options:
            if isinstance(option, dict) and option.get("type") == "effort":
                values = option.get("values")
                if isinstance(values, list):
                    levels.update(str(v).strip().lower() for v in values if isinstance(v, str))
        return frozenset(levels)
    if entry.get("reasoning") is False:
        return frozenset()
    # Reasoning model without published options: which efforts it takes is unknown.
    return None


def _build_index(raw: Any) -> _Index:
    index: _Index = {}
    if not isinstance(raw, dict):
        return index
    for provider, payload in raw.items():
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, dict):
            continue
        per_provider: dict[str, frozenset[str]] = {}
        for model_id, entry in models.items():
            if not isinstance(entry, dict):
                continue
            levels = _effort_levels(entry)
            if levels is not None:
                per_provider[str(model_id)] = levels
        index[str(provider)] = per_provider
    return index


def _load_index(path: Path) -> tuple[float, _Index] | None:
    """``(mtime, index)`` for *path*, memoized on mtime; ``None`` when unreadable."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    key = str(path)
    cached = _INDEX_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        log.debug("model_capabilities: unreadable models.dev copy %s", path, exc_info=True)
        return None
    loaded = (mtime, _build_index(raw))
    _INDEX_CACHE[key] = loaded
    return loaded


def _subset_for_cache(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fields this module reads, in the models.dev shape."""
    subset: dict[str, Any] = {}
    for provider, payload in raw.items():
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, dict):
            continue
        subset[provider] = {
            "models": {
                model_id: {
                    key: entry[key]
                    for key in ("reasoning", "reasoning_options")
                    if key in entry
                }
                for model_id, entry in models.items()
                if isinstance(entry, dict)
            }
        }
    return subset


def _fetch_models_dev(target: Path) -> bool:
    """Best-effort download into *target*; at most one attempt per TTL per process."""
    global _FETCH_ATTEMPTED_AT
    now = time.time()
    if not _network_allowed() or now - _FETCH_ATTEMPTED_AT < CACHE_TTL_SECONDS:
        return False
    _FETCH_ATTEMPTED_AT = now
    try:
        request = urllib.request.Request(MODELS_DEV_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            raw = json.loads(response.read().decode("utf-8"))
        if not isinstance(raw, dict):
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".models_dev.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(_subset_for_cache(raw), handle)
        os.replace(tmp_name, target)
        return True
    except Exception:
        log.debug("model_capabilities: models.dev fetch failed (offline is fine)", exc_info=True)
        return False


def models_dev_index() -> _Index | None:
    """The freshest usable models.dev effort index, else ``None``."""
    now = time.time()
    loaded = [
        result
        for result in (_load_index(_opencode_cache_path()), _load_index(_threnody_cache_path()))
        if result is not None
    ]
    fresh = [item for item in loaded if now - item[0] <= CACHE_TTL_SECONDS]
    if fresh:
        return max(fresh, key=lambda item: item[0])[1]
    target = _threnody_cache_path()
    if _fetch_models_dev(target):
        refreshed = _load_index(target)
        if refreshed is not None:
            return refreshed[1]
    if loaded:
        return max(loaded, key=lambda item: item[0])[1]
    return None


def models_dev_effort_levels(provider: str, model_id: str) -> frozenset[str] | None:
    """Effort values models.dev lists for *model_id* under *provider*.

    ``None`` = unknown (no catalog, model absent, or no published options);
    an empty set = the model is known to take no effort level.
    """
    index = models_dev_index()
    if index is None:
        return None
    return index.get(provider, {}).get(model_id)


def ordered_levels(levels: frozenset[str] | set[str]) -> list[str]:
    """*levels* weakest first; names outside the known scale sort last, by name."""
    rank = {name: index for index, name in enumerate(EFFORT_ORDER)}
    return sorted(levels, key=lambda name: (rank.get(name, len(rank)), name))


def copilot_effort_levels(model_id: str | None) -> frozenset[str] | None:
    """Levels ``gh copilot --effort`` may carry for *model_id*; ``None`` = unknown."""
    if not model_id:
        return None
    levels = models_dev_effort_levels("github-copilot", model_id)
    return None if levels is None else levels & COPILOT_CLI_EFFORT_LEVELS


def copilot_effort_accepted(model_id: str | None, effort: str | None) -> bool:
    """True when ``--effort <effort>`` is valid for *model_id* on Copilot."""
    if not effort:
        return False
    levels = copilot_effort_levels(model_id)
    if levels is None:
        log.debug("copilot: effort support for %r unknown; omitting --effort", model_id)
        return False
    return effort.strip().lower() in levels


def opencode_variant_levels(
    model_id: str | None,
    catalog: list[dict[str, Any]] | None = None,
) -> frozenset[str] | None:
    """Variant names ``opencode run --variant`` takes for *model_id*; ``None`` = unknown.

    The catalog row from ``opencode models --verbose`` is authoritative (OpenCode
    maps a budget-only model onto named variants itself, which models.dev does
    not show). Without one, models.dev is keyed on the id's provider prefix.
    """
    if not model_id:
        return None
    for row in catalog or ():
        if not isinstance(row, dict) or row.get("model_id") != model_id:
            continue
        levels = row.get("reasoning_levels")
        metadata = row.get("provider_metadata") if isinstance(row.get("provider_metadata"), dict) else {}
        if isinstance(levels, (list, tuple)) and (levels or metadata.get("variants_listed")):
            return frozenset(str(level).strip().lower() for level in levels if isinstance(level, str))
        break
    provider, _, bare = model_id.partition("/")
    if not bare:
        return None
    return models_dev_effort_levels(provider, bare)
