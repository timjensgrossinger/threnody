"""
mistral/providers.py — Mistral Vibe provider implementation for Threnody.

Implements command building, auth detection, and output cleaning for the
Mistral Vibe CLI (`vibe`).

Notes:
- The `vibe` CLI has no `--model` flag; the model is configured globally via
  `MISTRAL_API_KEY` or `~/.vibe/config.toml`. Tier model names are telemetry
  labels only and are not passed to the CLI.
- Non-interactive invocation: `vibe -p PROMPT --output text`
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from shared.discovery import DetectReason, ProviderReadiness

log = logging.getLogger(__name__)

_VIBE_CONFIG_PATH = Path.home() / ".vibe" / "config.toml"
_DEFAULT_TIER_MODELS = {
    "low": "devstral-small",
    "medium": "mistral-medium-3.5",
    "high": "mistral-medium-3.5",
}


def _normalize_model_name(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[-_\s]+", "-", value.strip().lower())


def _match_toml_string(key: str, line: str) -> str | None:
    match = re.match(rf"{re.escape(key)}\s*=\s*([\"'])(.*?)\1", line)
    if match:
        return match.group(2).strip()
    return None


def _parse_vibe_config_models(config_text: str) -> tuple[str | None, list[dict[str, str]]]:
    active_model: str | None = None
    models: list[dict[str, str]] = []
    current_model: dict[str, str] | None = None
    current_scope = "root"

    for raw_line in config_text.splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if re.match(r"\[\[\s*models\s*\]\]", line):
            if current_model:
                models.append(current_model)
            current_model = {}
            current_scope = "models"
            continue
        if line.startswith("["):
            if current_model:
                models.append(current_model)
                current_model = None
            current_scope = "other"
            continue
        if current_scope == "root":
            value = _match_toml_string("active_model", line)
            if value:
                active_model = value
            continue
        if current_scope != "models" or current_model is None:
            continue
        for key in ("name", "alias"):
            value = _match_toml_string(key, line)
            if value:
                current_model[key] = value

    if current_model:
        models.append(current_model)
    return active_model, models


def _display_model_name(model: dict[str, str]) -> str | None:
    alias = model.get("alias")
    if alias:
        return alias
    name = model.get("name")
    if name:
        return name
    return None


def _resolve_model_alias(model_ref: str | None, models: list[dict[str, str]]) -> str | None:
    normalized_ref = _normalize_model_name(model_ref)
    if not normalized_ref:
        return None
    for model in models:
        for key in ("alias", "name"):
            if _normalize_model_name(model.get(key)) == normalized_ref:
                return _display_model_name(model)
    return None


def _detect_mistral_tier_models(config_text: str) -> dict[str, str]:
    active_model, models = _parse_vibe_config_models(config_text)
    low_model = None
    for model in models:
        display = _display_model_name(model)
        normalized_display = _normalize_model_name(display)
        normalized_name = _normalize_model_name(model.get("name"))
        if normalized_display == "devstral-small" or normalized_name == "devstral-small-latest":
            low_model = display
            break
    medium_model = _resolve_model_alias(active_model, models)
    if _normalize_model_name(medium_model) == "devstral-small":
        medium_model = None
    if not medium_model:
        medium_model = _resolve_model_alias("mistral-medium-3.5", models)
    return {
        "low": low_model or _DEFAULT_TIER_MODELS["low"],
        "medium": medium_model or _DEFAULT_TIER_MODELS["medium"],
        "high": medium_model or _DEFAULT_TIER_MODELS["high"],
    }


def _mistral_workdir() -> str:
    candidate_bases = [Path(tempfile.gettempdir())]
    try:
        candidate_bases.insert(0, Path.home() / ".cache" / "Threnody" / "tmp")
    except RuntimeError:
        log.debug("Mistral Vibe: Path.home() unavailable for sandbox base", exc_info=True)
    for base in candidate_bases:
        try:
            base.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                base.chmod(0o700)
            except OSError:
                log.debug("Mistral Vibe: could not tighten workdir base permissions", exc_info=True)
            return tempfile.mkdtemp(prefix="threnody-vibe-", dir=str(base))
        except OSError:
            log.debug("Mistral Vibe: could not prepare sandbox base %s", base, exc_info=True)
    raise RuntimeError("Unable to create a private sandbox for Mistral Vibe")


def _build_mistral_command(
    provider: "Any",
    action: str,
    model: str,
    prompt: str,
    effort: str | None = None,
) -> list[str]:
    """Build a Mistral Vibe CLI command.

    The `vibe` CLI exposes no ``--model`` flag; the globally configured model
    is used. The ``model`` parameter is accepted for API compatibility but
    intentionally ignored here.

    ``--workdir`` points to a private sandbox directory instead of the caller's
    project so vibe does not scan and upload unrelated local files as context.

    Note on latency: default model is now Mistral Medium 3.5 (TTFT ~1.4s).
    CLI has known freeze bugs that hang indefinitely with no error. Router
    timeout is set to 300s to cap freeze duration. Mistral's own api_timeout
    default is 720s.
    """
    return ["vibe", "-p", prompt, "--output", "text", "--workdir", _mistral_workdir()]


def _detect_mistral(provider: "Any") -> ProviderReadiness:
    """Detect whether the Mistral Vibe CLI is installed and authenticated."""
    provider.tier_models.update(_DEFAULT_TIER_MODELS)
    if shutil.which(provider.binary) is None:
        return ProviderReadiness(
            routeable=False,
            reason=DetectReason.BINARY_MISSING,
        )

    config_present = False

    try:
        config_path = Path.home() / ".vibe" / "config.toml"
        if config_path.exists():
            raw = config_path.read_text(encoding="utf-8").strip()
            if raw:
                config_present = True
                provider.tier_models.update(_detect_mistral_tier_models(raw))
    except (OSError, RuntimeError, UnicodeDecodeError):
        log.debug("Mistral Vibe: config file unreadable", exc_info=True)

    api_key = os.getenv("MISTRAL_API_KEY", "").strip()
    if api_key:
        log.debug("Mistral Vibe: MISTRAL_API_KEY present → routeable")
        return ProviderReadiness(routeable=True, reason=DetectReason.READY)

    if config_present:
        log.debug("Mistral Vibe: ~/.vibe/config.toml present → routeable")
        return ProviderReadiness(routeable=True, reason=DetectReason.READY)

    log.debug("Mistral Vibe: no auth found → not routeable")
    return ProviderReadiness(routeable=False, reason=DetectReason.AUTH_FAILED)


def _clean_mistral_output(raw: str) -> str:
    """Strip Mistral Vibe preamble lines and return clean response text.

    The ``vibe --output text`` format typically returns the response directly,
    but may occasionally emit a leading status line starting with ``>``.
    """
    lines = raw.splitlines()
    if lines and lines[0].lstrip().startswith("> "):
        lines = lines[1:]
    return "\n".join(lines).strip()
