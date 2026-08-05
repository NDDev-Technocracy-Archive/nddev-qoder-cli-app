#!/usr/bin/env python3
"""Validate nddev-qoder-cli-app static public artifacts without side effects."""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:[-+].*)?\Z")
SETUP_IDS = ["nddev-builder"]
PROFILE_IDS = ["full-auto", "safe"]
HOST_IDS = ["macos-arm64", "macos-x64", "ubuntu-glibc-arm64", "ubuntu-glibc-x64"]
UNSUPPORTED = ["windows", "non-ubuntu-linux", "linux-musl", "unsupported-architecture"]
PLACEHOLDER_MARKER = "skele" + "ton"


def read_json(relative: str) -> dict[str, Any]:
    value = json.loads((ROOT / relative).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{relative} must contain a JSON object")
    return value


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def require_regular(relative: str, errors: list[str]) -> None:
    path = ROOT / relative
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        errors.append(f"missing required file: {relative}")
        return
    require(stat.S_ISREG(mode), f"required path is not a regular file: {relative}", errors)


def validate_release_closures(errors: list[str]) -> None:
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    required = {
        "README.md",
        "LICENSE",
        "VERSION",
        "AGENTS.md",
        ".claude",
        "build",
        "cli-tools",
        "config",
        "plugins",
        "profiles",
        "references",
        "setups",
    }
    for closure in ("archive_paths", "runtime_paths"):
        match = re.search(rf"(?m)^      {closure}: >-\n((?:        .+\n?)+)", release)
        members = set(match.group(1).split()) if match else set()
        require(required.issubset(members), f"release {closure} membership is incomplete", errors)


def validate_versions(errors: list[str]) -> None:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    build = read_json("build/version.json")
    manifest = read_json("build/manifest.json")
    contract = read_json("config/nddev-contract.json")
    baseline = read_json("references/qoder-cli-baseline.json")
    require(SEMVER.fullmatch(version) is not None, "VERSION is not SemVer", errors)
    require(build.get("schema_version") == 2, "build schema mismatch", errors)
    require(manifest.get("schema_version") == 2, "manifest schema mismatch", errors)
    require(contract.get("contract_version") == 2, "contract schema mismatch", errors)
    require(build.get("build_version") == version, "build version mismatch", errors)
    require(manifest.get("build_version") == version, "manifest version mismatch", errors)
    require(build.get("python_requires") == ">=3.9", "python requirement mismatch", errors)
    require(
        baseline.get("manifest", {}).get("latest") == build.get("qoder_cli_tested"),
        "baseline version mismatch",
        errors,
    )
    for owner, runtime in (
        ("manifest", manifest.get("runtime_compatibility", {})),
        ("contract", contract.get("runtime_compatibility", {})),
    ):
        require(
            runtime.get("tested_version") == build.get("qoder_cli_tested"),
            f"{owner} tested version mismatch",
            errors,
        )
        require(
            runtime.get("npm_package") == build.get("npm_package"),
            f"{owner} npm package mismatch",
            errors,
        )
        require(
            runtime.get("baseline_ref") == build.get("runtime_baseline_ref"),
            f"{owner} baseline mismatch",
            errors,
        )


def validate_assets(errors: list[str]) -> None:
    baseline = read_json("references/qoder-cli-baseline.json")
    manifest = baseline.get("manifest", {})
    files = manifest.get("files", {})
    require(isinstance(files, dict) and len(files) == 7, "asset catalog mismatch", errors)
    require("observed_at" not in baseline, "observation date must remain private", errors)
    for key in (
        "published_at",
        "release_page_total_downloads",
        "release_page_source_downloads",
        "observed_uploaded_runtime_asset_ids",
    ):
        require(key not in manifest, f"raw manifest observation must remain private: {key}", errors)
    require(
        not any(str(key).startswith("windows-") for key in files),
        "unsupported Windows asset observations must remain private",
        errors,
    )
    for key, asset in files.items():
        require(isinstance(asset, dict), f"asset is not an object: {key}", errors)
        if not isinstance(asset, dict):
            continue
        require(
            asset.get("filename") == Path(str(asset.get("url", ""))).name,
            f"asset filename mismatch: {key}",
            errors,
        )
        require(
            re.fullmatch(r"[0-9a-f]{64}", str(asset.get("sha256", ""))) is not None,
            f"asset digest mismatch: {key}",
            errors,
        )
        require(
            isinstance(asset.get("size"), int) and asset["size"] > 0,
            f"asset size mismatch: {key}",
            errors,
        )
    require(
        manifest.get("sha256") and re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"]) is not None,
        "manifest digest missing",
        errors,
    )
    installer = baseline.get("install_script", {})
    require(
        installer.get("sha256") and re.fullmatch(r"[0-9a-f]{64}", installer["sha256"]) is not None,
        "installer digest missing",
        errors,
    )
    require(installer.get("production_channel") is False, "installer trust policy mismatch", errors)


def validate_setup_profiles(errors: list[str]) -> None:
    manifest = read_json("build/manifest.json")
    contract = read_json("config/nddev-contract.json")
    setup = read_json("setups/nddev-builder/setup.json")
    settings = read_json("setups/nddev-builder/settings.json")
    require(manifest.get("setup_ids") == SETUP_IDS, "manifest setup ids mismatch", errors)
    require(manifest.get("profile_ids") == PROFILE_IDS, "manifest profile ids mismatch", errors)
    require(
        contract.get("setup_system", {}).get("setup_ids") == SETUP_IDS,
        "contract setup ids mismatch",
        errors,
    )
    require(
        contract.get("setup_system", {}).get("profile_ids") == PROFILE_IDS,
        "contract profile ids mismatch",
        errors,
    )
    require(setup.get("id") == "nddev-builder", "setup id mismatch", errors)
    require(setup.get("builder_projection") == "native-plugin", "setup projection mismatch", errors)
    require(
        settings.get("general", {}).get("enableAutoUpdate") is False,
        "autoupdate policy mismatch",
        errors,
    )
    require(
        settings.get("permissions", {}).get("trustDirectories") == [],
        "trust directory template mismatch",
        errors,
    )
    require(
        settings.get("enabledPlugins") == {"nddev-builder@local": True},
        "enabled plugin mapping mismatch",
        errors,
    )
    require(
        settings.get("hooksConfig") == {"enabled": True},
        "native hooksConfig mapping mismatch",
        errors,
    )
    require("hooks" not in settings, "legacy invalid hooks mapping is present", errors)
    expected = {
        "full-auto": ("bypass_permissions", ["--permission-mode", "bypass_permissions"]),
        "safe": ("dont_ask", ["--permission-mode", "dont_ask"]),
    }
    for profile_id, (mode, argv) in expected.items():
        profile = read_json(f"profiles/{profile_id}/profile.json")
        require(
            profile.get("permission_mode") == mode, f"{profile_id} permission mode mismatch", errors
        )
        require(profile.get("launch_args") == argv, f"{profile_id} launch argv mismatch", errors)


def validate_runtime_contract(errors: list[str]) -> None:
    manifest = read_json("build/manifest.json")
    contract = read_json("config/nddev-contract.json")
    baseline = read_json("references/qoder-cli-baseline.json")
    runtime = contract.get("runtime_launch", {})
    require(
        runtime.get("config_directory_environment_variable") == "QODER_CONFIG_DIR",
        "config environment mismatch",
        errors,
    )
    require(
        runtime.get("credential_environment_allowlist") == ["QODER_PERSONAL_ACCESS_TOKEN"],
        "credential allowlist mismatch",
        errors,
    )
    require(
        runtime.get("caller_home_credentials_inherited") is False,
        "credential inheritance mismatch",
        errors,
    )
    require(
        runtime.get("requires_managed_trust_directory") is True,
        "trust requirement mismatch",
        errors,
    )
    manifest_runtime = manifest.get("runtime_launch", {})
    for key, value in runtime.items():
        if key == "direct_command":
            continue
        require(manifest_runtime.get(key) == value, f"runtime launch mismatch: {key}", errors)
    require(
        manifest_runtime.get("blocked_environment_overrides")
        == [
            "QODER_* except QODER_PERSONAL_ACCESS_TOKEN",
            "HOME",
            "USERPROFILE",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "XDG_STATE_HOME",
            "TMPDIR",
        ],
        "blocked environment override policy mismatch",
        errors,
    )
    selection = baseline.get("product_host_selection", {})
    require(selection.get("supported_host_ids") == HOST_IDS, "supported hosts mismatch", errors)
    require(
        selection.get("unsupported_categories") == UNSUPPORTED, "unsupported hosts mismatch", errors
    )
    require(selection.get("ubuntu_version_floor") is None, "Ubuntu floor mismatch", errors)
    require(
        manifest.get("software_install") == contract.get("software_install"),
        "software surfaces diverge",
        errors,
    )
    builder = contract.get("builder_capability", {})
    require(
        builder.get("projection") == "qoder-native-local-plugin",
        "builder projection mismatch",
        errors,
    )
    require(builder.get("marketplace") is None, "marketplace policy mismatch", errors)
    require(builder.get("default_on") is True, "builder default mismatch", errors)
    for relative in builder.get("native_paths", []):
        require(
            (ROOT / "plugins" / "nddev-builder" / relative).exists(),
            f"missing plugin path: {relative}",
            errors,
        )


def validate_public_tree(errors: list[str]) -> None:
    require_regular("AGENTS.md", errors)
    require_regular(".claude/CLAUDE.md", errors)
    require_regular("setups/nddev-builder/AGENTS.md", errors)
    claude = ROOT / ".claude"
    try:
        require(stat.S_ISDIR(claude.lstat().st_mode), ".claude is not a real directory", errors)
        require(
            {path.name for path in claude.iterdir()} == {"CLAUDE.md"},
            ".claude contains unexpected entries",
            errors,
        )
    except FileNotFoundError:
        errors.append("missing required directory: .claude")
    if (claude / "CLAUDE.md").exists() and not (claude / "CLAUDE.md").is_symlink():
        require(
            (claude / "CLAUDE.md").read_text(encoding="utf-8") == "@../AGENTS.md\n",
            "Claude compatibility bridge mismatch",
            errors,
        )
    own_path = Path(__file__).resolve()
    for path in sorted(ROOT.rglob("*")):
        if path.is_dir() or ".git" in path.parts or path == own_path:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        require(
            PLACEHOLDER_MARKER not in text, f"placeholder found in {path.relative_to(ROOT)}", errors
        )


def main() -> int:
    errors: list[str] = []
    for check in (
        validate_release_closures,
        validate_versions,
        validate_assets,
        validate_setup_profiles,
        validate_runtime_contract,
        validate_public_tree,
    ):
        check(errors)
    if errors:
        for error in errors:
            print(f"error: {error}")
        return 1
    print("public contracts ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
