#!/usr/bin/env python3
"""Validate nddev-qoder-cli-app public contracts without side effects."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:[-+].*)?\Z")
EXPECTED = {
    "version": "1.1.5",
    "published_at": "2026-07-24T11:00:12.692Z",
    "command": "qodercli",
    "npm_package": "@qoder-ai/qodercli",
}
SETUP_IDS = ["safe", "balanced", "full-auto"]
LAUNCH_ARGS = {
    "safe": ["--permission-mode", "default"],
    "balanced": [
        "--permission-mode",
        "accept_edits",
        "--allowed-tools",
        "Read,Grep,Glob,Edit,Write,Bash(git status),Bash(git diff:*),Bash(git log:*)",
        "--disallowed-tools",
        "Bash(rm -rf:*),Bash(sudo:*),Bash(git push:*)",
    ],
    "full-auto": ["--permission-mode", "bypass_permissions"],
}
PLACEHOLDER_MARKER = "skele" + "ton"


def read_json(relative: str) -> dict[str, Any]:
    value = json.loads((ROOT / relative).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{relative} must contain a JSON object")
    return value


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def validate_versions(errors: list[str]) -> None:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    build = read_json("build/version.json")
    manifest = read_json("build/manifest.json")
    contract = read_json("config/nddev-contract.json")
    baseline = read_json("references/qoder-cli-baseline.json")
    require(SEMVER.fullmatch(version) is not None, "VERSION is not SemVer", errors)
    require(version != "0.0.0", "VERSION must not be placeholder 0.0.0", errors)
    require(build.get("schema_version") == 2, "build schema mismatch", errors)
    require(manifest.get("schema_version") == 2, "manifest schema mismatch", errors)
    require(contract.get("contract_version") == 2, "contract version mismatch", errors)
    require(build.get("build_version") == version, "build version mismatch", errors)
    require(manifest.get("build_version") == version, "manifest version mismatch", errors)
    require(PLACEHOLDER_MARKER not in contract, "contract contains placeholder marker", errors)
    require(build.get("qoder_cli_tested") == EXPECTED["version"], "tested version mismatch", errors)
    require(
        build.get("qoder_cli_published_at") == EXPECTED["published_at"],
        "published_at mismatch",
        errors,
    )
    require(build.get("command") == EXPECTED["command"], "command mismatch", errors)
    require(build.get("npm_package") == EXPECTED["npm_package"], "package mismatch", errors)
    manifest_baseline = baseline.get("manifest")
    npm = baseline.get("npm")
    require(isinstance(manifest_baseline, dict), "baseline manifest missing", errors)
    require(isinstance(npm, dict), "baseline npm missing", errors)
    if isinstance(manifest_baseline, dict):
        require(
            manifest_baseline.get("latest") == build.get("qoder_cli_tested"),
            "baseline latest mismatch",
            errors,
        )
        require(
            manifest_baseline.get("published_at") == build.get("qoder_cli_published_at"),
            "baseline published_at mismatch",
            errors,
        )
    if isinstance(npm, dict):
        require(npm.get("package") == build.get("npm_package"), "baseline package mismatch", errors)
        require(
            npm.get("version") == build.get("qoder_cli_tested"),
            "baseline package version mismatch",
            errors,
        )
        require(npm.get("binary") == build.get("command"), "baseline command mismatch", errors)
    for owner, runtime in (
        ("manifest", manifest.get("runtime_compatibility")),
        ("contract", contract.get("runtime_compatibility")),
    ):
        require(isinstance(runtime, dict), f"{owner} runtime_compatibility missing", errors)
        if isinstance(runtime, dict):
            require(
                runtime.get("tested_version") == build.get("qoder_cli_tested"),
                f"{owner} tested version mismatch",
                errors,
            )
            require(
                runtime.get("npm_package") == build.get("npm_package"),
                f"{owner} package mismatch",
                errors,
            )
            require(
                runtime.get("baseline_ref") == build.get("runtime_baseline_ref"),
                f"{owner} baseline ref mismatch",
                errors,
            )
    for owner, software in (
        ("manifest", manifest.get("software_install")),
        ("contract", contract.get("software_install")),
    ):
        require(isinstance(software, dict), f"{owner} software_install missing", errors)
        if isinstance(software, dict):
            installer = software.get("installer_evidence", {})
            bounds = software.get("bounds", {})
            require(
                installer.get("sha256")
                == "f26997301e5cf1ecff7378ffa25714ef0caf743a53f23530fdc61d4f08caaec2",
                f"{owner} installer evidence digest mismatch",
                errors,
            )
            require(installer.get("size") == 10652, f"{owner} installer size mismatch", errors)
            require(
                software.get("status_executes_binary") is False,
                f"{owner} software-status must not execute binary",
                errors,
            )
            require(
                software.get("stage_version_probe") is True,
                f"{owner} stage version probe missing",
                errors,
            )
            require(software.get("target_owned") is True, f"{owner} target ownership missing", errors)
            require(
                bounds == {
                    "max_download_bytes": 268435456,
                    "max_tree_bytes": 536870912,
                    "max_tree_paths": 5000,
                },
                f"{owner} software bounds mismatch",
                errors,
            )


def validate_assets(errors: list[str]) -> None:
    baseline = read_json("references/qoder-cli-baseline.json")
    installer = baseline.get("install_script", {})
    require(
        installer.get("sha256") == "f26997301e5cf1ecff7378ffa25714ef0caf743a53f23530fdc61d4f08caaec2",
        "installer digest mismatch",
        errors,
    )
    require(installer.get("size") == 10652, "installer size mismatch", errors)
    files = baseline.get("manifest", {}).get("files")
    require(isinstance(files, dict), "baseline files missing", errors)
    expected = {
        "darwin-arm64": ("48b220d90ba69462b24fb214a5cf1ecc9e3d4785e7d8e32e63ceb9ca739b9d8a", 37716918),
        "darwin-amd64": ("9c72803f4c883af971b43c968023b925f8c085e79672ac2c66d8a65e80a571d4", 41711896),
        "linux-arm64": ("ad5ed775e8719781df40add6b815f7c823bbe1640dcba45d846145b8c8d8b88d", 49845139),
        "linux-arm64-musl": ("2ea1b1edc121bc747e88f9bdf6740622db18035768b7033f5ed6b5d659e9d6fb", 48537619),
        "linux-amd64": ("72f4b210bf6a9f854e7dc85dba1bddc935eae4f1efff41da1ced0bf330ae2dee", 50417468),
        "linux-amd64-baseline": ("2ee5797c634dc2277a16b64cc5f1faf855c3d2ee497bb86c18e449803a4d3328", 50043332),
        "linux-amd64-musl": ("90ff9351c24a45787450551bdbb6352e4d9ad16299be84021729f9e2d0e6945f", 49176709),
        "windows-amd64": ("f0970f29e525b18e3be67ea4692ec2dde073f65a95eec9ad0b81e07b32d1b62c", 59903461),
        "windows-amd64-legacy": ("ed160f0075a8ac048ea34a6e92ed047739957c4bca8020821cb2bfe3c521fc9e", 55343806),
    }
    if isinstance(files, dict):
        for key, (digest, size) in expected.items():
            asset = files.get(key)
            require(isinstance(asset, dict), f"asset missing: {key}", errors)
            if isinstance(asset, dict):
                require(asset.get("sha256") == digest, f"asset digest mismatch: {key}", errors)
                require(asset.get("size") == size, f"asset size mismatch: {key}", errors)
                require(
                    str(asset.get("url", "")).startswith(
                        "https://qoder-ide.oss-accelerate.aliyuncs.com/qodercli/releases/1.1.5/"
                    ),
                    f"asset URL mismatch: {key}",
                    errors,
                )


def validate_setups(errors: list[str]) -> None:
    manifest = read_json("build/manifest.json")
    contract = read_json("config/nddev-contract.json")
    require(manifest.get("setup_ids") == SETUP_IDS, "manifest setup ids mismatch", errors)
    setup_system = contract.get("setup_system")
    require(isinstance(setup_system, dict), "contract setup_system missing", errors)
    if isinstance(setup_system, dict):
        require(setup_system.get("setup_ids") == SETUP_IDS, "contract setup ids mismatch", errors)
        require(setup_system.get("builder_default_on") is True, "builder default mismatch", errors)
    for setup_id in SETUP_IDS:
        metadata = read_json(f"setups/{setup_id}/setup.json")
        settings = read_json(f"setups/{setup_id}/settings.json")
        require(metadata.get("id") == setup_id, f"{setup_id} id mismatch", errors)
        require(
            metadata.get("launch_args") == LAUNCH_ARGS[setup_id],
            f"{setup_id} launch args mismatch",
            errors,
        )
        require(
            metadata.get("builder_default_on") is True, f"{setup_id} builder not default-on", errors
        )
        require(
            metadata.get("builder_projection") == "native-plugin-plus-user-files",
            f"{setup_id} builder projection mismatch",
            errors,
        )
        require(
            settings.get("general", {}).get("enableAutoUpdate") is False,
            f"{setup_id} update must be disabled",
            errors,
        )
        require(
            settings.get("enabledPlugins") == {"nddev-builder@local": True},
            f"{setup_id} builder plugin must be enabled",
            errors,
        )
        require(
            settings.get("extraKnownMarketplaces") == {},
            f"{setup_id} marketplace must be empty",
            errors,
        )
        require(
            settings.get("sandbox", {}).get("gitAuth") is False,
            f"{setup_id} git auth must be off",
            errors,
        )
        require(
            settings.get("sandbox", {}).get("ghAuth") is False,
            f"{setup_id} gh auth must be off",
            errors,
        )


def validate_builder(errors: list[str]) -> None:
    build = read_json("build/version.json")
    contract = read_json("config/nddev-contract.json")
    plugin = read_json("plugins/nddev-builder/.qoder-plugin/plugin.json")
    hooks = read_json("plugins/nddev-builder/hooks/hooks.json")
    builder = contract.get("builder_capability")
    require(plugin.get("name") == "nddev-builder", "builder name mismatch", errors)
    require(
        plugin.get("version") == build.get("nddev_builder_plugin_version"),
        "builder version mismatch",
        errors,
    )
    require(plugin.get("skills") == "skills", "builder skills path mismatch", errors)
    require(plugin.get("agents") == "agents", "builder agents path mismatch", errors)
    require(plugin.get("hooks") == "hooks/hooks.json", "builder hooks path mismatch", errors)
    require(isinstance(hooks.get("hooks"), dict), "builder hooks must use Qoder hook object", errors)
    require("Stop" in hooks.get("hooks", {}), "builder Stop hook missing", errors)
    require(isinstance(builder, dict), "contract builder missing", errors)
    if isinstance(builder, dict):
        require(
            builder.get("projection") == "qoder-native-plugin-user-files",
            "builder projection mismatch",
            errors,
        )
        require(builder.get("default_on") is True, "builder default_on mismatch", errors)
        require(builder.get("marketplace") is None, "builder marketplace must be null", errors)
    for relative in (
        "plugins/nddev-builder/.qoder-plugin/plugin.json",
        "plugins/nddev-builder/skills/nddev-builder/SKILL.md",
        "plugins/nddev-builder/agents/nddev-builder.md",
        "plugins/nddev-builder/hooks/hooks.json",
    ):
        require((ROOT / relative).is_file(), f"missing builder native file: {relative}", errors)


def validate_public_tree(errors: list[str]) -> None:
    own_path = Path(__file__).resolve()
    for path in sorted(ROOT.rglob("*")):
        if path.is_dir() or ".git" in path.parts or "__pycache__" in path.parts:
            continue
        if path.resolve() == own_path:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        if PLACEHOLDER_MARKER in text:
            errors.append(f"placeholder marker found in {path.relative_to(ROOT)}")


def main() -> int:
    errors: list[str] = []
    try:
        validate_versions(errors)
        validate_assets(errors)
        validate_setups(errors)
        validate_builder(errors)
        validate_public_tree(errors)
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))
    if errors:
        print(f"validate_public_contracts.py: FAIL ({len(errors)} error(s))")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("validate_public_contracts.py: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
