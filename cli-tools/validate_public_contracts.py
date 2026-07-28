#!/usr/bin/env python3
"""Validate nddev-qoder-cli-app public contracts without persistent side effects."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:[-+].*)?\Z")
EXPECTED = {
    "build_version": "0.2.0",
    "qoder_version": "1.1.7",
    "published_at": "2026-07-28T10:58:43.023Z",
    "command": "qodercli",
    "npm_package": "@qoder-ai/qodercli",
    "manifest_sha256": "f5aaf3ddcb5bba307173db5a51a72298c20f9cf2d32ba4e8f10b6e80b9cb6594",
    "manifest_size": 2717,
    "install_sha256": "1e850fa3375bdccd179215f91638f4065d912c5dbddbd346d0ba352bad8ca9be",
    "install_size": 10651,
}
SETUP_IDS = ["nddev-builder"]
PROFILE_IDS = ["full-auto", "safe"]
SUPPORTED_HOST_IDS = ["macos-arm64", "macos-x64", "ubuntu-glibc-arm64", "ubuntu-glibc-x64"]
UNSUPPORTED_CATEGORIES = ["windows", "non-ubuntu-linux", "linux-musl", "unsupported-architecture"]
MANAGED_FILES = [
    "settings.json",
    "AGENTS.md",
    "plugins/nddev-builder/.qoder-plugin/plugin.json",
    "plugins/nddev-builder/skills/nddev-builder/SKILL.md",
    "plugins/nddev-builder/agents/nddev-builder.md",
    "plugins/nddev-builder/hooks/hooks.json",
    "NDDEV-QODER-CLI-SETUP.json",
]
PLACEHOLDER_MARKER = "skele" + "ton"


def read_json(relative: str) -> dict[str, Any]:
    value = json.loads((ROOT / relative).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{relative} must contain a JSON object")
    return value


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def load_manager() -> Any:
    spec = importlib.util.spec_from_file_location(
        "nddev_qoder_cli_public_contract_manager",
        ROOT / "cli-tools" / "nddev_qoder_cli.py",
    )
    if spec is None or spec.loader is None:
        raise AssertionError("cannot import public manager")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def validate_versions(errors: list[str]) -> None:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    build = read_json("build/version.json")
    manifest = read_json("build/manifest.json")
    contract = read_json("config/nddev-contract.json")
    baseline = read_json("references/qoder-cli-baseline.json")
    require(SEMVER.fullmatch(version) is not None, "VERSION is not SemVer", errors)
    require(version == EXPECTED["build_version"], "VERSION mismatch", errors)
    require(build.get("schema_version") == 2, "build schema mismatch", errors)
    require(manifest.get("schema_version") == 2, "manifest schema mismatch", errors)
    require(contract.get("contract_version") == 2, "contract version mismatch", errors)
    require(build.get("build_version") == version, "build version mismatch", errors)
    require(manifest.get("build_version") == version, "manifest version mismatch", errors)
    require(build.get("python_requires") == ">=3.9", "python_requires mismatch", errors)
    require(PLACEHOLDER_MARKER not in json.dumps(contract), "contract contains placeholder marker", errors)
    require(build.get("qoder_cli_tested") == EXPECTED["qoder_version"], "tested version mismatch", errors)
    require(build.get("qoder_cli_published_at") == EXPECTED["published_at"], "published_at mismatch", errors)
    require(build.get("command") == EXPECTED["command"], "command mismatch", errors)
    require(build.get("npm_package") == EXPECTED["npm_package"], "npm package mismatch", errors)
    manifest_baseline = baseline.get("manifest", {})
    require(manifest_baseline.get("latest") == EXPECTED["qoder_version"], "baseline latest mismatch", errors)
    require(manifest_baseline.get("published_at") == EXPECTED["published_at"], "baseline published_at mismatch", errors)
    require(manifest_baseline.get("sha256") == EXPECTED["manifest_sha256"], "manifest digest mismatch", errors)
    require(manifest_baseline.get("size") == EXPECTED["manifest_size"], "manifest size mismatch", errors)
    installer = baseline.get("install_script", {})
    require(installer.get("sha256") == EXPECTED["install_sha256"], "installer digest mismatch", errors)
    require(installer.get("size") == EXPECTED["install_size"], "installer size mismatch", errors)
    for owner, runtime in (("manifest", manifest.get("runtime_compatibility")), ("contract", contract.get("runtime_compatibility"))):
        require(isinstance(runtime, dict), f"{owner} runtime_compatibility missing", errors)
        if isinstance(runtime, dict):
            require(runtime.get("tested_version") == EXPECTED["qoder_version"], f"{owner} tested version mismatch", errors)
            require(runtime.get("npm_package") == EXPECTED["npm_package"], f"{owner} npm package mismatch", errors)
            require(runtime.get("baseline_ref") == build.get("runtime_baseline_ref"), f"{owner} baseline ref mismatch", errors)


def validate_assets(errors: list[str]) -> None:
    baseline = read_json("references/qoder-cli-baseline.json")
    files = baseline.get("manifest", {}).get("files")
    require(isinstance(files, dict), "baseline files missing", errors)
    expected = {
        "darwin-arm64": ("qodercli-darwin-arm64.tar.gz", "a91892460b337d8ca3ae2a2789d679a7c9722369200dc3db36c07c0f7db6e273", 37888191),
        "darwin-amd64": ("qodercli-darwin-x64.tar.gz", "8134472d822fac09fa8967699bc35d3693fb0e70b03132ddf75775a599a4795d", 41890101),
        "linux-arm64-musl": ("qodercli-linux-arm64-musl.tar.gz", "16777e6135de3601085488dec9cd0c608ee50ece82d0bb007cd60ee3a4a5dcae", 48714463),
        "linux-arm64": ("qodercli-linux-arm64.tar.gz", "3bff6443dd82198b165cb4154ead7ea428bc3640203e75b9641feaf60a8a5d3a", 50004403),
        "linux-amd64-baseline": ("qodercli-linux-x64-baseline.tar.gz", "b5a3ef827e82283230928c4d91d98acf288309161785bdb4a700993a466eebcb", 50218319),
        "linux-amd64-musl": ("qodercli-linux-x64-musl.tar.gz", "0d76b3af8a4236c8ffef535f43dd6b703901c0fad6dbcbc7bfeb26cf5a351e70", 49345269),
        "linux-amd64": ("qodercli-linux-x64.tar.gz", "027006a42eafe4e6badd9906d9986927dffadb98b73a3bb8df72e07f2ec4723b", 50589197),
        "windows-amd64-legacy": ("qodercli-windows-x64-legacy.zip", "98c784be96f894d016fa766528a13759ed68e108c0a69cd9d2194590f08c4e83", 55338658),
        "windows-amd64": ("qodercli-windows-x64.zip", "c41b44855f2fd800299e94a451a8c6d589e82dbd27a3083525dd6981842315eb", 60070506),
    }
    if isinstance(files, dict):
        require(sorted(files) == sorted(expected), "baseline asset id set mismatch", errors)
        for key, (filename, digest, size) in expected.items():
            asset = files.get(key)
            require(isinstance(asset, dict), f"asset missing: {key}", errors)
            if isinstance(asset, dict):
                require(asset.get("filename") == filename, f"asset filename mismatch: {key}", errors)
                require(asset.get("sha256") == digest, f"asset digest mismatch: {key}", errors)
                require(asset.get("size") == size, f"asset size mismatch: {key}", errors)
                require(str(asset.get("url", "")).startswith("https://qoder-ide.oss-accelerate.aliyuncs.com/qodercli/releases/1.1.7/"), f"asset URL mismatch: {key}", errors)
    release_sources = baseline.get("manifest", {}).get("release_page_source_downloads")
    require(baseline.get("manifest", {}).get("release_page_total_downloads") == 14, "release page count mismatch", errors)
    require(release_sources == ["source-code.zip", "source-code.tar.gz"], "source archive split mismatch", errors)


def validate_setup_profiles(errors: list[str]) -> None:
    manifest = read_json("build/manifest.json")
    contract = read_json("config/nddev-contract.json")
    require(manifest.get("setup_ids") == SETUP_IDS, "manifest setup ids mismatch", errors)
    require(manifest.get("profile_ids") == PROFILE_IDS, "manifest profile ids mismatch", errors)
    setup_system = contract.get("setup_system", {})
    require(setup_system.get("setup_ids") == SETUP_IDS, "contract setup ids mismatch", errors)
    require(setup_system.get("profile_ids") == PROFILE_IDS, "contract profile ids mismatch", errors)
    setup = read_json("setups/nddev-builder/setup.json")
    settings = read_json("setups/nddev-builder/settings.json")
    require(setup.get("id") == "nddev-builder", "setup id mismatch", errors)
    require(setup.get("builder_projection") == "native-plugin", "builder projection mismatch", errors)
    require(settings.get("general", {}).get("enableAutoUpdate") is False, "autoupdate must be disabled", errors)
    require(settings.get("permissions", {}).get("trustDirectories") == [], "setup trustDirectories must be manager-filled", errors)
    require(settings.get("enabledPlugins") == {"nddev-builder@local": True}, "builder plugin must be enabled", errors)
    profiles = {profile_id: read_json(f"profiles/{profile_id}/profile.json") for profile_id in PROFILE_IDS}
    require(profiles["full-auto"].get("permission_mode") == "bypass_permissions", "full-auto mode mismatch", errors)
    require(profiles["full-auto"].get("launch_args") == ["--permission-mode", "bypass_permissions"], "full-auto argv mismatch", errors)
    require(profiles["safe"].get("permission_mode") == "dont_ask", "safe mode mismatch", errors)
    require(profiles["safe"].get("launch_args") == ["--permission-mode", "dont_ask"], "safe argv mismatch", errors)


def validate_runtime_contract(errors: list[str]) -> None:
    manifest = read_json("build/manifest.json")
    contract = read_json("config/nddev-contract.json")
    baseline = read_json("references/qoder-cli-baseline.json")
    require(manifest.get("managed_files") == MANAGED_FILES, "manifest managed file set mismatch", errors)
    runtime = contract.get("runtime_launch", {})
    require(runtime.get("config_directory_environment_variable") == "QODER_CONFIG_DIR", "QODER_CONFIG_DIR contract mismatch", errors)
    require(runtime.get("credential_environment_allowlist") == ["QODER_PERSONAL_ACCESS_TOKEN"], "credential allowlist mismatch", errors)
    require(runtime.get("caller_home_credentials_inherited") is False, "caller home inheritance mismatch", errors)
    require(runtime.get("requires_managed_trust_directory") is True, "trusted directory requirement mismatch", errors)
    transaction = contract.get("transaction_policy", {})
    require(transaction.get("adr") == "docs/adr/0003-transactional-lifecycle-contract.md@15361ed", "ADR pin mismatch", errors)
    require(transaction.get("read_only_no_create") is True, "read-only no-create mismatch", errors)
    selection = baseline.get("product_host_selection", {})
    require(selection.get("supported_host_ids") == SUPPORTED_HOST_IDS, "supported host ids mismatch", errors)
    require(selection.get("unsupported_categories") == UNSUPPORTED_CATEGORIES, "unsupported categories mismatch", errors)
    require(selection.get("ubuntu_version_floor") is None, "ubuntu version floor must be null", errors)
    require(selection.get("ubuntu_floor_source") == "no-official-floor", "ubuntu floor source mismatch", errors)
    software = manifest.get("software_install", {})
    require(software.get("supported_product_hosts") == SUPPORTED_HOST_IDS, "software host ids mismatch", errors)
    require(software.get("unsupported_categories") == UNSUPPORTED_CATEGORIES, "software unsupported categories mismatch", errors)
    require(software.get("ubuntu_version_floor") is None, "software ubuntu floor must be null", errors)


def validate_manager_surface(errors: list[str]) -> None:
    try:
        manager = load_manager()
        require(manager.TESTED_VERSION == EXPECTED["qoder_version"], "manager tested version mismatch", errors)
        require(manager.DEFAULT_SETUP_ID == "nddev-builder", "manager default setup mismatch", errors)
        require(manager.DEFAULT_PROFILE_ID == "full-auto", "manager default profile mismatch", errors)
        require(manager.ALLOWED_CREDENTIAL_ENV == ("QODER_PERSONAL_ACCESS_TOKEN",), "manager credential allowlist mismatch", errors)
        require("QODER_MEMORY" not in manager.ALLOWED_CREDENTIAL_ENV, "manager must not allow QODER_MEMORY", errors)
        manager.parse_args(["list", "--json"])
        manager.parse_args(["update", "--target", "/tmp/example", "--json"])
        manager.parse_args(["remove-cli", "--target", "/tmp/example", "--json"])
    except Exception as exc:
        errors.append(f"manager import/parse failed: {exc}")


def validate_public_smoke(errors: list[str]) -> None:
    manager = ROOT / "cli-tools" / "nddev_qoder_cli.py"
    with tempfile.TemporaryDirectory(prefix="nddev-qoder-public-contract-") as tmp:
        base = Path(tmp)
        locks = base / "locks"
        home = base / "home"
        parent = base / "parent"
        workspace = base / "workspace"
        for directory in (locks, home, parent, workspace):
            directory.mkdir(mode=0o700)
            directory.chmod(0o700)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home),
            "USERPROFILE": str(home),
            "TMPDIR": str(locks),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        target = parent / "target"
        commands = [
            ["list", "--json"],
            ["status", "--target", str(target), "--json"],
            ["plan", "--target", str(target), "--workspace", str(workspace), "--json"],
        ]
        for command in commands:
            completed = subprocess.run(
                [sys.executable, "-B", str(manager), *command],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=20,
            )
            if completed.returncode != 0:
                errors.append(f"public smoke failed {command}: {completed.stdout} {completed.stderr}")
                continue
            try:
                payload = json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                errors.append(f"public smoke returned non-JSON {command}: {exc}")
                continue
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                errors.append(f"public smoke returned non-ok payload {command}: {payload!r}")
        residue = sorted(path.relative_to(base).as_posix() for path in base.rglob("*"))
        allowed = {"home", "locks", "parent", "workspace"}
        if set(residue) - allowed:
            errors.append(f"read-only smoke left residue: {residue}")


def validate_no_cache_residue(errors: list[str]) -> None:
    residue = [
        path.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("*")
        if path.name == "__pycache__" or path.suffix == ".pyc"
    ]
    require(not residue, f"cache residue present: {residue}", errors)


def main() -> int:
    errors: list[str] = []
    validate_versions(errors)
    validate_assets(errors)
    validate_setup_profiles(errors)
    validate_runtime_contract(errors)
    validate_manager_surface(errors)
    validate_public_smoke(errors)
    validate_no_cache_residue(errors)
    if errors:
        for error in errors:
            print(f"error: {error}")
        return 1
    print("public contracts ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
