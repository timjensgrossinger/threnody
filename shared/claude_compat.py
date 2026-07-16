"""Load the hyphenated Claude adapter directory from source or a wheel."""
from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
from types import ModuleType


def load_claude_module(module_name: str) -> ModuleType:
    """Load a Claude adapter module under a valid Python package name."""
    qualified_name = f"claude_code.{module_name}"
    legacy_name = f"claude-code.{module_name}"
    existing = sys.modules.get(qualified_name) or sys.modules.get(legacy_name)
    if existing is not None:
        sys.modules[qualified_name] = existing
        sys.modules[legacy_name] = existing
        return existing

    package_root = Path(__file__).resolve().parent.parent
    candidates = (
        package_root / "claude-code" / f"{module_name}.py",
        package_root / "claude_code" / f"{module_name}.py",
    )
    module_path = next((path for path in candidates if path.is_file()), None)
    if module_path is None:
        raise ModuleNotFoundError(f"Claude adapter module not found: {module_name}")

    spec = spec_from_file_location(qualified_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load Claude adapter module: {module_name}")
    module = module_from_spec(spec)
    sys.modules[qualified_name] = module
    sys.modules[legacy_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(qualified_name, None)
        sys.modules.pop(legacy_name, None)
        raise
    return module
