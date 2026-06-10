from __future__ import annotations

from collections.abc import Mapping
import logging
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - depends on host environment
    yaml = None

try:
    import questionary
    from rich.console import Console
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table  # noqa: F401 — available for callers
    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HOST_CLIS = [
    "claude-code",
    "github-copilot",
    "github-copilot-cli",
    "gemini-cli",
    "codex",
    "cursor",
    "opencode",
]

FALLBACK_PROVIDERS: list[dict] = [
    {
        "name": "github-copilot",
        "available": True,
        "routeable": True,
        "models": {"low": "gpt-5-mini", "medium": "gpt-5.4", "high": "gpt-5.4"},
        "billing": "subscription",
    },
    {
        "name": "claude-code",
        "available": True,
        "routeable": True,
        "models": {"low": "haiku", "medium": "sonnet", "high": "opus"},
        "billing": "subscription",
    },
    {
        "name": "gemini-cli",
        "available": True,
        "routeable": True,
        "models": {
            "low": "gemini-2.5-flash-lite",
            "medium": "gemini-2.5-flash",
            "high": "gemini-2.5-pro",
        },
        "billing": "subscription",
    },
    {
        "name": "codex",
        "available": True,
        "routeable": True,
        "models": {"low": "o4-mini", "medium": "o3", "high": "o4"},
        "billing": "subscription",
    },
    {
        "name": "cursor",
        "available": True,
        "routeable": True,
        "models": {"low": "claude-haiku", "medium": "claude-sonnet", "high": "claude-opus"},
        "billing": "subscription",
    },
    {
        "name": "opencode",
        "available": True,
        "routeable": True,
        "models": {"low": "opencode/nemotron-3-super-free"},
        "billing": "subscription",
    },
]

# ---------------------------------------------------------------------------
# Config path — import from shared.config with fallback
# ---------------------------------------------------------------------------

try:
    from shared.config import CONFIG_YAML as _DEFAULT_CONFIG_PATH
    from shared.config import _load_basic_yaml_mapping
    from shared.config import SUPPORTED_ROUTING_POLICY_SHELLS
except Exception:
    _DEFAULT_CONFIG_PATH: Path = Path.home() / ".local/lib/threnody/config.yaml"  # type: ignore[assignment]
    SUPPORTED_ROUTING_POLICY_SHELLS = (  # type: ignore[assignment]
        "claude-code",
        "github-copilot-cli",
        "gemini-cli",
        "cursor",
        "codex",
    )

    def _load_basic_yaml_mapping(text: str) -> dict:  # type: ignore[no-redef]
        return {}

BASE_DIR = Path("~/.local/lib/threnody").expanduser()


# ---------------------------------------------------------------------------
# Provider loading
# ---------------------------------------------------------------------------

def _load_providers() -> list[dict]:
    providers_path = BASE_DIR / "providers.json"
    fallback_by_name = {p["name"]: p for p in FALLBACK_PROVIDERS}
    if providers_path.exists():
        import json
        try:
            data = json.loads(providers_path.read_text())
            raw = data.get("providers", [])
            if not isinstance(raw, list):
                return FALLBACK_PROVIDERS
            # Merge live availability data with static model/billing info
            merged = []
            for p in raw:
                if not isinstance(p, Mapping):
                    continue
                name = p.get("name", "")
                base = dict(fallback_by_name.get(name, {"name": name, "models": {}, "billing": "unknown"}))
                base.update({k: v for k, v in p.items() if v is not None})
                if not isinstance(base.get("models"), Mapping):
                    base["models"] = {}
                merged.append(base)
            return merged
        except Exception:
            log.debug("Failed to parse providers.json, using fallback", exc_info=True)
    return FALLBACK_PROVIDERS


def _provider_models(p: Mapping) -> Mapping:
    models = p.get("models", {})
    if isinstance(models, Mapping):
        return models
    if isinstance(models, list):
        projection: dict[str, str] = {}
        for entry in models:
            if not isinstance(entry, Mapping):
                continue
            tier = entry.get("tier")
            model_id = entry.get("model_id") or entry.get("id")
            if (
                tier in {"low", "medium", "high"}
                and isinstance(model_id, str)
                and entry.get("available", True) is not False
                and entry.get("deprecated", False) is not True
                and tier not in projection
            ):
                projection[str(tier)] = model_id
        return projection
    return {}


def _provider_label(p: dict) -> str:
    models = _provider_models(p)
    model_str = " / ".join(str(models[t]) for t in ("low", "medium", "high") if models.get(t))
    return f"{p.get('name', '(unknown)')}  ({p.get('billing', 'unknown')} · {model_str})"


def _format_yaml_scalar(value: object) -> str:
    import json

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


def _dump_simple_yaml(value: object, *, indent: int = 0) -> str:
    pad = " " * indent
    if isinstance(value, Mapping):
        lines: list[str] = []
        for key, item in value.items():
            rendered_key = str(key)
            if isinstance(item, Mapping):
                if item:
                    lines.append(f"{pad}{rendered_key}:")
                    lines.append(_dump_simple_yaml(item, indent=indent + 2).rstrip())
                else:
                    lines.append(f"{pad}{rendered_key}: {{}}")
            elif isinstance(item, list):
                if item:
                    lines.append(f"{pad}{rendered_key}:")
                    lines.append(_dump_simple_yaml(item, indent=indent + 2).rstrip())
                else:
                    lines.append(f"{pad}{rendered_key}: []")
            else:
                lines.append(f"{pad}{rendered_key}: {_format_yaml_scalar(item)}")
        return "\n".join(lines) + ("\n" if lines else "")
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, Mapping):
                lines.append(f"{pad}-")
                lines.append(_dump_simple_yaml(item, indent=indent + 2).rstrip())
            elif isinstance(item, list):
                lines.append(f"{pad}-")
                lines.append(_dump_simple_yaml(item, indent=indent + 2).rstrip())
            else:
                lines.append(f"{pad}- {_format_yaml_scalar(item)}")
        return "\n".join(lines) + ("\n" if lines else "")
    return f"{pad}{_format_yaml_scalar(value)}\n"


def _dump_yaml(data: Mapping) -> str:
    if yaml is not None:
        return yaml.dump(data, default_flow_style=False, sort_keys=False)
    return _dump_simple_yaml(data)


def _load_yaml_mapping(path: Path) -> dict:
    if not path.exists():
        return {}
    if yaml is not None:
        loaded = yaml.safe_load(path.read_text()) or {}
    else:
        loaded = _load_basic_yaml_mapping(path.read_text(encoding="utf-8"))
    return dict(loaded) if isinstance(loaded, Mapping) else {}


# ---------------------------------------------------------------------------
# Page 1 — Provider selection
# ---------------------------------------------------------------------------

def _page1_rich(providers: list[dict]) -> list[str]:
    console = Console()
    console.print(
        Panel(
            "[bold]Step 1/4 — Provider Selection[/bold]",
            subtitle="Select which providers Threnody may use",
            style="blue",
        )
    )
    available = [p for p in providers if p.get("available", True)]
    choices = [
        questionary.Choice(
            title=_provider_label(p),
            value=p.get("name", ""),
            checked=p.get("routeable", True),
        )
        for p in available
        if p.get("name")
    ]
    result = questionary.checkbox("Enabled providers:", choices=choices).ask()
    if result is None:
        raise KeyboardInterrupt
    return result


def _page1_plain(providers: list[dict]) -> list[str]:
    available = [p for p in providers if p.get("available", True)]
    print("\n=== Step 1/4 — Provider Selection ===")
    print("Select which providers Threnody may use.\n")
    for i, p in enumerate(available, 1):
        marker = "[*]" if p.get("routeable", True) else "[ ]"
        print(f"  {i}. {marker} {_provider_label(p)}")
    print()
    raw = input("Enter numbers to enable (comma-separated, blank=keep marked): ").strip()
    if not raw:
        return [p.get("name", "") for p in available if p.get("routeable", True) and p.get("name")]
    selected_indices = {int(x.strip()) - 1 for x in raw.split(",") if x.strip().isdigit()}
    return [available[i].get("name", "") for i in sorted(selected_indices) if 0 <= i < len(available) and available[i].get("name")]


# ---------------------------------------------------------------------------
# Page 2 — Per-caller routing
# ---------------------------------------------------------------------------

def _page2_rich(enabled: list[str]) -> dict[str, list[str]]:
    callers = [p for p in enabled if p in HOST_CLIS]
    if len(callers) < 2:
        return {}
    console = Console()
    console.print(
        Panel(
            "[bold]Step 2/4 — Per-Caller Routing[/bold]",
            subtitle="Restrict which providers each caller may route to",
            style="blue",
        )
    )
    allowlists: dict[str, list[str]] = {}
    for caller in callers:
        choices = [
            questionary.Choice(title=p, value=p, checked=True) for p in enabled
        ]
        result = questionary.checkbox(
            f"[{caller}] → which providers may it route to?",
            choices=choices,
        ).ask()
        if result is None:
            raise KeyboardInterrupt
        # only record when user actually restricted something
        if set(result) != set(enabled):
            allowlists[caller] = result
    return allowlists


def _page2_plain(enabled: list[str]) -> dict[str, list[str]]:
    callers = [p for p in enabled if p in HOST_CLIS]
    if len(callers) < 2:
        return {}
    print("\n=== Step 2/4 — Per-Caller Routing ===")
    print("Restrict which providers each caller may route to.")
    allowlists: dict[str, list[str]] = {}
    for caller in callers:
        print(f"\n  [{caller}] → which providers may it route to?")
        for i, p in enumerate(enabled, 1):
            print(f"    {i}. [*] {p}")
        raw = input("  Enter numbers to allow (blank=all): ").strip()
        if not raw:
            continue
        selected_indices = {int(x.strip()) - 1 for x in raw.split(",") if x.strip().isdigit()}
        selected = [enabled[i] for i in sorted(selected_indices) if 0 <= i < len(enabled)]
        if set(selected) != set(enabled):
            allowlists[caller] = selected
    return allowlists


# ---------------------------------------------------------------------------
# Page 3 — Tier preferences
# ---------------------------------------------------------------------------

def _page3_rich(enabled: list[str], providers: list[dict]) -> dict[str, list[dict]]:
    console = Console()
    console.print(
        Panel(
            "[bold]Step 3/4 — Tier Preferences[/bold]",
            subtitle="Optionally pin a preferred provider per tier",
            style="blue",
        )
    )
    proceed = questionary.confirm(
        "Set preferred provider per tier? (optional)", default=False
    ).ask()
    if not proceed:
        return {}
    provider_map = {p.get("name", ""): p for p in providers if p.get("name")}
    preferred: dict[str, list[dict]] = {}
    for tier in ("low", "medium", "high"):
        tier_providers = [
            p for p in enabled
            if p in provider_map and _provider_models(provider_map[p]).get(tier)
        ]
        if not tier_providers:
            continue
        choices = [
            questionary.Choice(
                title=f"{p}  ({_provider_models(provider_map[p]).get(tier, '')})",
                value=p,
            )
            for p in tier_providers
        ]
        result = questionary.select(
            f"Preferred provider for [{tier}] tier:", choices=choices
        ).ask()
        if result is None:
            raise KeyboardInterrupt
        model = _provider_models(provider_map[result]).get(tier, "")
        preferred[tier] = [{"provider": result, "model": model}]
    return preferred


def _page3_plain(enabled: list[str], providers: list[dict]) -> dict[str, list[dict]]:
    print("\n=== Step 3/4 — Tier Preferences ===")
    raw = input("Set preferred provider per tier? (y/N): ").strip().lower()
    if raw not in ("y", "yes"):
        return {}
    provider_map = {p.get("name", ""): p for p in providers if p.get("name")}
    preferred: dict[str, list[dict]] = {}
    for tier in ("low", "medium", "high"):
        tier_providers = [
            p for p in enabled
            if p in provider_map and _provider_models(provider_map[p]).get(tier)
        ]
        if not tier_providers:
            continue
        print(f"\n  Preferred provider for [{tier}] tier:")
        for i, p in enumerate(tier_providers, 1):
            model = _provider_models(provider_map[p]).get(tier, "")
            print(f"    {i}. {p}  ({model})")
        raw2 = input(f"  Enter number (1-{len(tier_providers)}): ").strip()
        if raw2.isdigit():
            idx = int(raw2) - 1
            if 0 <= idx < len(tier_providers):
                chosen = tier_providers[idx]
                model = _provider_models(provider_map[chosen]).get(tier, "")
                preferred[tier] = [{"provider": chosen, "model": model}]
    return preferred



def _is_float(s: str) -> bool:
    try:
        float(s)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Page 3.5 — Usage Window Thresholds
# ---------------------------------------------------------------------------

def _page_usage_windows_rich(enabled: list[str]) -> dict[str, list[dict]]:
    console = Console()
    console.print(
        Panel(
            "[bold]Step 3.5/4 — Usage Window Thresholds[/bold]",
            subtitle="Reroute providers that approach their token budget",
            style="blue",
        )
    )
    configure = questionary.confirm(
        "Configure usage-window thresholds? (optional)", default=False
    ).ask()
    if not configure:
        return {}

    results: dict[str, list[dict]] = {}
    for provider in enabled:
        add = questionary.confirm(f"Add threshold for {provider}?", default=False).ask()
        if not add:
            continue

        while True:
            hrs = questionary.text("Hours (e.g. 5 or 168):").ask()
            if hrs is None:
                raise KeyboardInterrupt
            if _is_float(hrs) and float(hrs) > 0:
                hours = float(hrs)
                break
            console.print("[red]Enter a positive number.[/red]")

        bt = questionary.text("Budget tokens (int, blank = None):").ask()
        if bt is None:
            raise KeyboardInterrupt
        budget_tokens: int | None = None
        if bt.strip():
            try:
                budget_tokens = int(bt.strip())
            except ValueError:
                console.print("[yellow]Invalid int — budget_tokens set to None.[/yellow]")

        while True:
            th = questionary.text("Threshold 0.0–1.0 [default 0.8]:", default="0.8").ask()
            if th is None:
                raise KeyboardInterrupt
            if _is_float(th) and 0.0 <= float(th) <= 1.0:
                threshold = float(th)
                break
            console.print("[red]Enter a float between 0.0 and 1.0.[/red]")

        action = questionary.select(
            "Action:",
            choices=["cost_rank_boost", "prefer_alternatives", "hard_exclude"],
        ).ask()
        if action is None:
            raise KeyboardInterrupt

        results.setdefault(provider, []).append({
            "hours": hours,
            "budget_tokens": budget_tokens,
            "threshold": threshold,
            "action": action,
        })
    return results


def _page_usage_windows_plain(enabled: list[str]) -> dict[str, list[dict]]:
    print("\n=== Step 3.5/4 — Usage Window Thresholds ===")
    ans = input("Configure usage-window thresholds? (y/N): ").strip().lower()
    if ans not in ("y", "yes"):
        return {}

    results: dict[str, list[dict]] = {}
    for provider in enabled:
        if input(f"Add threshold for {provider}? (y/N): ").strip().lower() not in ("y", "yes"):
            continue

        while True:
            s = input("Hours (float): ").strip()
            try:
                hours = float(s)
                if hours > 0:
                    break
            except Exception:
                pass
            print("Enter a positive number.")

        s = input("Budget tokens (int, blank = None): ").strip()
        budget_tokens: int | None = None
        if s:
            try:
                budget_tokens = int(s)
            except ValueError:
                print("Invalid int — budget_tokens set to None.")

        while True:
            s = input("Threshold 0.0–1.0 [default 0.8]: ").strip() or "0.8"
            try:
                threshold = float(s)
                if 0.0 <= threshold <= 1.0:
                    break
            except Exception:
                pass
            print("Enter a float between 0.0 and 1.0.")

        actions = ["cost_rank_boost", "prefer_alternatives", "hard_exclude"]
        while True:
            for i, a in enumerate(actions, 1):
                print(f"  {i}. {a}")
            sel = input("Choose action (1-3): ").strip()
            if sel.isdigit() and 1 <= int(sel) <= len(actions):
                action = actions[int(sel) - 1]
                break
            print("Enter 1, 2, or 3.")

        results.setdefault(provider, []).append({
            "hours": hours,
            "budget_tokens": budget_tokens,
            "threshold": threshold,
            "action": action,
        })
    return results



# ---------------------------------------------------------------------------
# Page 3.75 — Routing enforcement policy
# ---------------------------------------------------------------------------

def _page_routing_policy_rich() -> dict:
    console = Console()
    console.print(
        Panel(
            "[bold]Step 3.75/4 — Routing Enforcement[/bold]",
            subtitle="Choose strict or advisory routing instructions per AI shell",
            style="blue",
        )
    )
    mode = questionary.select(
        "Routing enforcement preference:",
        choices=[
            questionary.Choice(
                "Recommended defaults (Claude strict, Copilot/Gemini/Cursor/Codex advisory)",
                value="default",
            ),
            questionary.Choice("Strict for all AI shells", value="strict"),
            questionary.Choice("Advisory for all AI shells", value="advisory"),
            questionary.Choice("Custom per shell", value="custom"),
        ],
    ).ask()
    if mode is None:
        raise KeyboardInterrupt
    policy: dict = {"mode": mode}
    if mode != "custom":
        return policy

    shells: dict[str, dict[str, str]] = {}
    for shell_id in SUPPORTED_ROUTING_POLICY_SHELLS:
        shell_mode = questionary.select(
            f"{shell_id} routing mode:",
            choices=[
                questionary.Choice("Recommended default", value="default"),
                questionary.Choice("Strict", value="strict"),
                questionary.Choice("Advisory", value="advisory"),
            ],
        ).ask()
        if shell_mode is None:
            raise KeyboardInterrupt
        shells[shell_id] = {"mode": shell_mode}
    policy["shells"] = shells
    return policy


def _page_routing_policy_plain() -> dict:
    print("\n=== Step 3.75/4 — Routing Enforcement ===")
    choices = [
        ("default", "Recommended defaults (Claude strict, Copilot/Gemini/Cursor/Codex advisory)"),
        ("strict", "Strict for all AI shells"),
        ("advisory", "Advisory for all AI shells"),
        ("custom", "Custom per shell"),
    ]
    for i, (_, label) in enumerate(choices, 1):
        print(f"  {i}. {label}")
    raw = input("Choose routing enforcement (1-4, blank=1): ").strip()
    mode = choices[int(raw) - 1][0] if raw.isdigit() and 1 <= int(raw) <= len(choices) else "default"
    policy: dict = {"mode": mode}
    if mode != "custom":
        return policy

    shells: dict[str, dict[str, str]] = {}
    for shell_id in SUPPORTED_ROUTING_POLICY_SHELLS:
        raw_shell = input(f"  {shell_id} mode [default/strict/advisory, blank=default]: ").strip().lower()
        shell_mode = raw_shell if raw_shell in {"default", "strict", "advisory"} else "default"
        shells[shell_id] = {"mode": shell_mode}
    policy["shells"] = shells
    return policy


# ---------------------------------------------------------------------------
# Write config
# ---------------------------------------------------------------------------

def _write_config(
    config_path: Path,
    disabled: list[str],
    caller_allowlists: dict[str, list[str]],
    preferred_routing: dict[str, list[dict]],
    routing_policy: dict,
    usage_windows: dict[str, list[dict]] | None = None,
) -> None:
    existing = _load_yaml_mapping(config_path)
    providers_section: dict = existing.setdefault("providers", {})
    if disabled:
        providers_section["disabled"] = sorted(disabled)
    else:
        providers_section.pop("disabled", None)
    if caller_allowlists:
        providers_section["caller_allowlists"] = caller_allowlists
    if preferred_routing:
        providers_section["preferred_routing"] = preferred_routing
    if usage_windows:
        providers_section["usage_windows"] = usage_windows
    else:
        providers_section.pop("usage_windows", None)
    existing["routing_policy"] = routing_policy
    config_path.write_text(_dump_yaml(existing), encoding="utf-8")
    log.debug("Config written to %s", config_path)


# ---------------------------------------------------------------------------
# Page 4 — Review and confirm
# ---------------------------------------------------------------------------

def _page4_rich(
    config_path: Path,
    enabled: list[str],
    all_available: list[str],
    caller_allowlists: dict[str, list[str]],
    preferred_routing: dict[str, list[dict]],
    routing_policy: dict,
    usage_windows: dict[str, list[dict]] | None = None,
) -> bool:
    disabled = [p for p in all_available if p not in enabled]
    preview: dict = {"providers": {}}
    if disabled:
        preview["providers"]["disabled"] = sorted(disabled)
    if caller_allowlists:
        preview["providers"]["caller_allowlists"] = caller_allowlists
    if preferred_routing:
        preview["providers"]["preferred_routing"] = preferred_routing
    preview["routing_policy"] = routing_policy

    console = Console()
    console.print(
        Panel(
            "[bold]Step 4/4 — Review & Confirm[/bold]",
            subtitle=f"Will write: {config_path}",
            style="blue",
        )
    )
    preview_yaml = _dump_yaml(preview)
    console.print(Syntax(preview_yaml, "yaml", theme="monokai"))

    confirm = questionary.confirm("Write config.yaml?", default=True).ask()
    if not confirm:
        console.print("Aborted — no changes made.")
        return False
    _write_config(config_path, disabled, caller_allowlists, preferred_routing, routing_policy, usage_windows)
    console.print(f"[green]Config written to {config_path}[/green]")
    return True


def _page4_plain(
    config_path: Path,
    enabled: list[str],
    all_available: list[str],
    caller_allowlists: dict[str, list[str]],
    preferred_routing: dict[str, list[dict]],
    routing_policy: dict,
    usage_windows: dict[str, list[dict]] | None = None,
) -> bool:
    disabled = [p for p in all_available if p not in enabled]
    preview: dict = {"providers": {}}
    if disabled:
        preview["providers"]["disabled"] = sorted(disabled)
    if caller_allowlists:
        preview["providers"]["caller_allowlists"] = caller_allowlists
    if preferred_routing:
        preview["providers"]["preferred_routing"] = preferred_routing
    preview["routing_policy"] = routing_policy

    print("\n=== Step 4/4 — Review & Confirm ===")
    print(f"Will write: {config_path}\n")
    print(_dump_yaml(preview))

    raw = input("Write config.yaml? (Y/n): ").strip().lower()
    if raw in ("n", "no"):
        print("Aborted — no changes made.")
        return False
    _write_config(config_path, disabled, caller_allowlists, preferred_routing, routing_policy, usage_windows)
    print(f"Config written to {config_path}")
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_wizard(config_path: Path | None = None) -> bool:
    """Launch the settings wizard. Returns True if config was written."""
    if config_path is None:
        config_path = _DEFAULT_CONFIG_PATH

    providers = _load_providers()
    all_available = [p.get("name", "") for p in providers if p.get("available", True) and p.get("name")]

    try:
        if HAS_DEPS:
            enabled = _page1_rich(providers)
            if not enabled:
                Console().print("[yellow]No providers selected — aborting.[/yellow]")
                return False
            caller_allowlists = _page2_rich(enabled)
            preferred_routing = _page3_rich(enabled, providers)
            usage_windows = _page_usage_windows_rich(enabled)
            routing_policy = _page_routing_policy_rich()
            return _page4_rich(
                config_path, enabled, all_available, caller_allowlists, preferred_routing, routing_policy, usage_windows
            )
        else:
            enabled = _page1_plain(providers)
            if not enabled:
                print("No providers selected — aborting.")
                return False
            caller_allowlists = _page2_plain(enabled)
            preferred_routing = _page3_plain(enabled, providers)
            usage_windows = _page_usage_windows_plain(enabled)
            routing_policy = _page_routing_policy_plain()
            return _page4_plain(
                config_path, enabled, all_available, caller_allowlists, preferred_routing, routing_policy, usage_windows
            )
    except KeyboardInterrupt:
        if HAS_DEPS:
            Console().print("\n[yellow]Wizard cancelled.[/yellow]")
        else:
            print("\nWizard cancelled.")
        return False


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    _path: Path | None = None
    if len(sys.argv) > 1:
        # Resolve and constrain to BASE_DIR to prevent path traversal (CWE-22)
        _candidate = BASE_DIR.joinpath(sys.argv[1]).resolve()
        if not str(_candidate).startswith(str(BASE_DIR.resolve())):
            print(f"Error: config path must be inside {BASE_DIR}", file=sys.stderr)
            sys.exit(2)
        _path = _candidate
    sys.exit(0 if run_wizard(_path) else 1)
