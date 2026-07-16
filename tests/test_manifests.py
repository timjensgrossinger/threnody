"""Focused coverage for deterministic distribution manifest generation."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "build-manifests.py"
OUTPUTS = (
    "server.json",
    ".claude-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
    "smithery.yaml",
)


def _read_json(relative_path: str) -> dict:
    with (ROOT / relative_path).open(encoding="utf-8") as handle:
        return json.load(handle)


def test_generated_manifests_preserve_canonical_metadata() -> None:
    manifest = _read_json("threnody.manifest.json")
    server = _read_json("server.json")
    plugin = _read_json(".claude-plugin/plugin.json")
    marketplace = _read_json(".claude-plugin/marketplace.json")
    smithery = yaml.safe_load((ROOT / "smithery.yaml").read_text(encoding="utf-8"))

    assert server["name"] == manifest["registry_name"]
    assert server["version"] == manifest["version"]
    assert server["description"] == manifest["description"]
    package = server["packages"][0]
    assert package["identifier"] == manifest["pypi_package"]
    assert package["version"] == manifest["pypi_version"]
    assert package["transport"]["type"] == manifest["entry_command"]["transport"]
    assert package["runtimeHint"] == manifest["runtime_hint"]
    assert [item["name"] for item in package["environmentVariables"]] == [
        property_schema["env"]
        for property_schema in manifest["config_schema"]["properties"].values()
    ]
    assert server["_meta"]["io.modelcontextprotocol.registry/publisher-provided"] == {
        "tags": manifest["tags"],
        "license": manifest["license"],
    }

    assert plugin["name"] == manifest["name"]
    assert plugin["version"] == manifest["version"]
    assert plugin["author"] == manifest["author"]
    assert plugin["mcpServers"] == manifest["capabilities"]["plugin"]["mcp_servers"]
    assert plugin["skills"] == manifest["capabilities"]["plugin"]["skills"]

    marketplace_plugin = marketplace["plugins"][0]
    assert marketplace["metadata"]["version"] == manifest["version"]
    assert marketplace_plugin["version"] == manifest["version"]
    assert marketplace_plugin["category"] == manifest["category"]
    assert marketplace_plugin["tags"] == manifest["tags"]
    assert marketplace_plugin["mcpServers"] == plugin["mcpServers"]

    assert smithery["startCommand"]["type"] == manifest["entry_command"]["transport"]
    assert smithery["startCommand"]["configSchema"]["required"] == manifest[
        "config_schema"
    ]["required"]
    assert smithery["startCommand"]["configSchema"]["properties"].keys() == (
        manifest["config_schema"]["properties"].keys()
    )
    assert "commandFunction" in smithery["startCommand"]


def test_manifest_generation_is_deterministic() -> None:
    before = {path: (ROOT / path).read_bytes() for path in OUTPUTS}
    subprocess.run([sys.executable, str(SCRIPT)], cwd=ROOT, check=True)
    after = {path: (ROOT / path).read_bytes() for path in OUTPUTS}
    assert after == before


def test_check_mode_detects_drift_without_rewriting_it(tmp_path: Path) -> None:
    for relative_path in ("threnody.manifest.json", *OUTPUTS):
        source = ROOT / relative_path
        destination = tmp_path / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    target = tmp_path / "server.json"
    target.write_bytes(target.read_bytes() + b"drift\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "server.json" in result.stderr
    assert target.read_bytes().endswith(b"drift\n")


def test_check_mode_accepts_generated_outputs() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
