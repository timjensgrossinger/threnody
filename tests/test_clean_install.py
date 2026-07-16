"""Smoke-test wheel and sdist installs without repository or host-CLI state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import venv

import pytest


ROOT = Path(__file__).resolve().parent.parent


def _pep440_version(value: str) -> str:
    """Normalize the repository's ``0.3.0-alpha.2`` style for package metadata."""
    match = re.fullmatch(r"(?P<base>\d+(?:\.\d+)*)(?:-(?P<stage>alpha|beta|rc)\.(?P<num>\d+))?", value)
    if match is None:
        raise AssertionError(f"VERSION is not a supported release format: {value!r}")
    stage = {"alpha": "a", "beta": "b", "rc": "rc"}.get(match.group("stage"), "")
    return f"{match.group('base')}{stage}{match.group('num') or ''}"


EXPECTED_PACKAGE_VERSION = _pep440_version(
    (ROOT / "VERSION").read_text(encoding="utf-8").strip()
)
EXPECTED_DISPLAY_VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
EXPECTED_METADATA_VERSION = EXPECTED_PACKAGE_VERSION
INITIALIZE_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "clean-install-test", "version": "1"},
    },
}

if sys.version_info < (3, 10) or sys.version_info >= (3, 14):
    pytest.skip(
        "clean-install smoke requires a supported Python 3.10-3.13 interpreter; "
        "the package metadata intentionally rejects interpreters outside that range",
        allow_module_level=True,
    )


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command with captured text output and a useful failure message."""
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )


def _venv_python(venv_dir: Path) -> Path:
    """Return the platform-specific Python executable in a virtualenv."""
    executable = "python.exe" if os.name == "nt" else "python"
    directory = "Scripts" if os.name == "nt" else "bin"
    return venv_dir / directory / executable


def _clean_runtime_env(venv_dir: Path, tmp_path: Path) -> dict[str, str]:
    """Remove repository and host state while retaining system commands."""
    env = {
        name: value
        for name, value in os.environ.items()
        if not any(
            marker in name.upper()
            for marker in ("TOKEN", "PASSWORD", "SECRET", "PRIVATE_KEY")
        )
    }
    env.pop("PYTHONPATH", None)
    for name in (
        "CLAUDE_CODE",
        "CLAUDE_CODE_SESSION",
        "COPILOT_CLI",
        "COPILOT_RUN_APP",
        "OPENCODE_HOST",
        "OPENCODE_SESSION",
    ):
        env.pop(name, None)
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "PATH": os.pathsep.join(
                [str(_venv_python(venv_dir).parent), "/usr/bin", "/bin"]
            ),
            "THRENODY_ALLOW_NO_HOST": "1",
            "THRENODY_INSTALL_DIR": str(tmp_path / "install"),
            "THRENODY_SKIP_WIZARD": "1",
            "THRENODY_TEST_MODE": "1",
        }
    )
    return env


def _build_distributions(output_dir: Path) -> tuple[Path, Path]:
    """Build both release formats into a temporary directory."""
    _run(
        [
            sys.executable,
            "-m",
            "build",
            "--sdist",
            "--wheel",
            "--outdir",
            str(output_dir),
        ],
        cwd=ROOT,
    )
    wheels = list(output_dir.glob("*.whl"))
    sdists = list(output_dir.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise AssertionError(
            f"expected one wheel and one sdist, found wheels={wheels}, sdists={sdists}"
        )
    return wheels[0], sdists[0]


@pytest.fixture(scope="module")
def built_distributions(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path]:
    artifacts_dir = tmp_path_factory.mktemp("artifacts")
    return _build_distributions(artifacts_dir)


@pytest.mark.parametrize("artifact_name", ["wheel", "sdist"])
def test_clean_install_answers_initialize(
    artifact_name: str,
    built_distributions: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    """Install each artifact in a fresh venv and complete the MCP handshake."""
    wheel, sdist = built_distributions
    artifact = wheel if artifact_name == "wheel" else sdist

    venv_dir = tmp_path / f"{artifact_name}-venv"
    venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
    python = _venv_python(venv_dir)
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            str(artifact),
        ],
        cwd=tmp_path,
        env=_clean_runtime_env(venv_dir, tmp_path),
    )

    metadata = _run(
        [
            str(python),
            "-c",
            (
                "import importlib.metadata as m; "
                "import threnody; "
                "from shared.version import get_version; "
                "print(m.version('threnody-mcp')); "
                "print(threnody.__version__); "
                "print(get_version())"
            ),
        ],
        cwd=tmp_path,
        env=_clean_runtime_env(venv_dir, tmp_path),
    ).stdout.splitlines()
    assert metadata == [
        EXPECTED_METADATA_VERSION,
        EXPECTED_DISPLAY_VERSION,
        EXPECTED_DISPLAY_VERSION,
    ]

    server = venv_dir / ("Scripts" if os.name == "nt" else "bin") / "threnody-mcp"
    assert server.is_file()
    result = subprocess.run(
        [str(server)],
        input=json.dumps(INITIALIZE_REQUEST) + "\n",
        cwd=tmp_path,
        env=_clean_runtime_env(venv_dir, tmp_path),
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    response = next((item for item in responses if item.get("id") == 1), None)
    assert response is not None, (
        f"initialize response missing; stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    assert response["result"]["protocolVersion"] == "2024-11-05"
    assert response["result"]["serverInfo"] == {
        "name": "Threnody",
        "version": EXPECTED_DISPLAY_VERSION,
    }
