#!/usr/bin/env python3
"""Generate distribution manifests from the canonical Threnody manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


CANONICAL_FILE = "threnody.manifest.json"
OUTPUT_FILES = (
    "server.json",
    ".claude-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
    "smithery.yaml",
)


def _json_bytes(value: dict[str, Any]) -> bytes:
    """Return stable, human-readable JSON bytes."""
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _command(manifest: dict[str, Any]) -> dict[str, Any]:
    return manifest["entry_command"]


def _mcp_server(manifest: dict[str, Any]) -> dict[str, Any]:
    command = _command(manifest)
    return {
        "command": command["command"],
        "args": command["args"],
    }


def _environment_variables(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    schema = manifest["config_schema"]
    required = set(schema["required"])
    variables: list[dict[str, Any]] = []
    for name, property_schema in schema["properties"].items():
        variable: dict[str, Any] = {
            "name": property_schema["env"],
            "description": property_schema["description"],
            "isRequired": name in required,
        }
        if "default" in property_schema:
            default = property_schema["default"]
            if property_schema["type"] == "boolean":
                default = "1" if default else "0"
            variable["default"] = str(default)
        variables.append(variable)
    return variables


def _server_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    command = _command(manifest)
    return {
        "$schema": (
            "https://static.modelcontextprotocol.io/schemas/"
            "2025-12-11/server.schema.json"
        ),
        "name": manifest["registry_name"],
        "title": manifest["name"].title(),
        "description": manifest["description"],
        "version": manifest["version"],
        "websiteUrl": manifest["homepage"],
        "repository": manifest["repository"],
        "packages": [
            {
                "registryType": "pypi",
                "registryBaseUrl": "https://pypi.org",
                "identifier": manifest["pypi_package"],
                "version": manifest["pypi_version"],
                "transport": {"type": command["transport"]},
                "runtimeHint": manifest["runtime_hint"],
                "environmentVariables": _environment_variables(manifest),
            }
        ],
        "_meta": {
            "io.modelcontextprotocol.registry/publisher-provided": {
                "tags": manifest["tags"],
                "license": manifest["license"],
            }
        },
    }


def _plugin_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    plugin_capabilities = manifest["capabilities"]["plugin"]
    return {
        "name": manifest["name"],
        "description": manifest["description"],
        "version": manifest["version"],
        "author": manifest["author"],
        "homepage": manifest["homepage"],
        "license": manifest["license"],
        "mcpServers": plugin_capabilities["mcp_servers"],
        "skills": plugin_capabilities["skills"],
    }


def _marketplace_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    plugin_capabilities = manifest["capabilities"]["plugin"]
    return {
        "name": manifest["name"],
        "owner": manifest["author"],
        "metadata": {
            "description": manifest["description"],
            "version": manifest["version"],
        },
        "plugins": [
            {
                "name": manifest["name"],
                "source": ".",
                "description": manifest["description"],
                "version": manifest["version"],
                "homepage": manifest["homepage"],
                "repository": manifest["repository"]["url"],
                "license": manifest["license"],
                "category": manifest["category"],
                "tags": manifest["tags"],
                "mcpServers": plugin_capabilities["mcp_servers"],
            }
        ],
    }


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _smithery_command_function(manifest: dict[str, Any]) -> list[str]:
    command = _command(manifest)
    lines = ["    (config) => ({", f"      command: {_yaml_scalar(command['command'])},"]
    args = ", ".join(_yaml_scalar(argument) for argument in command["args"])
    lines.append(f"      args: [{args}],")
    lines.append("      env: {")

    for name, property_schema in manifest["config_schema"]["properties"].items():
        env_name = property_schema["env"]
        if property_schema["type"] == "string":
            lines.append(
                "        "
                f"...(config.{name} ? {{ {env_name}: config.{name} }} : {{}}),"
            )
        elif property_schema["type"] == "boolean":
            true_value = property_schema.get("env_true", "1")
            false_value = property_schema.get("env_false", "0")
            if property_schema.get("default") is True:
                expression = (
                    f"config.{name} === false ? {_yaml_scalar(false_value)} : "
                    f"{_yaml_scalar(true_value)}"
                )
            else:
                expression = (
                    f"config.{name} ? {_yaml_scalar(true_value)} : "
                    f"{_yaml_scalar(false_value)}"
                )
            lines.append(f"        {env_name}: {expression},")
        else:
            lines.append(f"        {env_name}: config.{name},")

    lines.extend(["      }", "    })"])
    return lines


def _smithery_manifest(manifest: dict[str, Any]) -> bytes:
    schema = manifest["config_schema"]
    lines = [
        "startCommand:",
        f"  type: {_yaml_scalar(_command(manifest)['transport'])}",
        "  configSchema:",
        f"    type: {_yaml_scalar(schema['type'])}",
        "    required: "
        + (
            "[]"
            if not schema["required"]
            else "["
            + ", ".join(_yaml_scalar(name) for name in schema["required"])
            + "]"
        ),
        "    properties:",
    ]
    for name, property_schema in schema["properties"].items():
        lines.extend(
            [
                f"      {name}:",
                f"        type: {_yaml_scalar(property_schema['type'])}",
            ]
        )
        if "default" in property_schema:
            lines.append(f"        default: {_yaml_scalar(property_schema['default'])}")
        lines.append(
            f"        description: {_yaml_scalar(property_schema['description'])}"
        )

    lines.append("  commandFunction: |")
    lines.extend(_smithery_command_function(manifest))
    lines.extend(["runtime: python", ""])
    return "\n".join(lines).encode("utf-8")


def build_outputs(manifest: dict[str, Any]) -> dict[str, bytes]:
    """Build every generated distribution manifest in memory."""
    return {
        "server.json": _json_bytes(_server_manifest(manifest)),
        ".claude-plugin/plugin.json": _json_bytes(_plugin_manifest(manifest)),
        ".claude-plugin/marketplace.json": _json_bytes(
            _marketplace_manifest(manifest)
        ),
        "smithery.yaml": _smithery_manifest(manifest),
    }


def _load_manifest(root: Path) -> dict[str, Any]:
    with (root / CANONICAL_FILE).open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, dict):
        raise ValueError(f"{CANONICAL_FILE} must contain a JSON object")
    required_keys = {
        "name",
        "registry_name",
        "pypi_package",
        "version",
        "pypi_version",
        "description",
        "homepage",
        "repository",
        "license",
        "author",
        "entry_command",
        "runtime_hint",
        "config_schema",
        "capabilities",
        "tags",
        "category",
    }
    missing = sorted(required_keys - manifest.keys())
    if missing:
        raise ValueError(f"{CANONICAL_FILE} is missing keys: {', '.join(missing)}")
    entry_command = manifest["entry_command"]
    if (
        not isinstance(entry_command, dict)
        or not isinstance(entry_command.get("command"), str)
        or not isinstance(entry_command.get("transport"), str)
        or not isinstance(entry_command.get("args"), list)
        or not all(isinstance(argument, str) for argument in entry_command["args"])
    ):
        raise ValueError("entry_command must contain command, args, and transport")
    config_schema = manifest["config_schema"]
    if (
        not isinstance(config_schema, dict)
        or not isinstance(config_schema.get("type"), str)
        or not isinstance(config_schema.get("required"), list)
        or not all(isinstance(name, str) for name in config_schema["required"])
        or not isinstance(config_schema.get("properties"), dict)
    ):
        raise ValueError("config_schema.properties must be an object")
    for name, property_schema in config_schema["properties"].items():
        if (
            not isinstance(name, str)
            or not isinstance(property_schema, dict)
            or not isinstance(property_schema.get("type"), str)
        ):
            raise ValueError(f"config_schema.properties.{name} must be an object")
        if not isinstance(property_schema.get("env"), str):
            raise ValueError(f"config_schema.properties.{name}.env is required")
        if not isinstance(property_schema.get("description"), str):
            raise ValueError(
                f"config_schema.properties.{name}.description is required"
            )
    return manifest


def _write_or_check(root: Path, outputs: dict[str, bytes], check: bool) -> int:
    drifted: list[str] = []
    for relative_path, expected in outputs.items():
        path = root / relative_path
        actual = path.read_bytes() if path.exists() else None
        if check:
            if actual != expected:
                drifted.append(relative_path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(expected)

    if drifted:
        print(
            "Generated manifests are out of date; run "
            "python3 scripts/build-manifests.py:",
            file=sys.stderr,
        )
        for path in drifted:
            print(f"  {path}", file=sys.stderr)
        return 1
    if check:
        print("Generated manifests are up to date.")
    else:
        print(f"Generated {len(outputs)} manifests.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Generate manifests or verify that checked-in outputs have no drift."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when generated files differ from the canonical manifest",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    try:
        manifest = _load_manifest(args.root)
        return _write_or_check(args.root, build_outputs(manifest), args.check)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"manifest generation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
