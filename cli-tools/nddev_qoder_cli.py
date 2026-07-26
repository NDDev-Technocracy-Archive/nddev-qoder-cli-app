#!/usr/bin/env python3
"""Transactional setup manager for an explicit Qoder CLI home."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parents[1]
CATALOG_ROOT = ROOT / "setups"
BUILDER_ROOT = ROOT / "plugins" / "nddev-builder"
VERSION = (ROOT / "VERSION").read_text(encoding="ascii").strip()
PRODUCT_NAME = "nddev-qoder-cli-app"
COMMAND_NAME = "qodercli"
STAMP_NAME = "NDDEV-QODER-CLI-SETUP.json"
BACKUP_NAME = "NDDEV-QODER-CLI-BACKUP.json"
BASELINE_REF = ROOT / "references" / "qoder-cli-baseline.json"
TESTED_VERSION = "1.1.5"
NPM_PACKAGE = "@qoder-ai/qodercli"
INSTALL_SCRIPT_URL = "https://qoder.com/install"
INSTALL_SCRIPT_SHA256 = "f26997301e5cf1ecff7378ffa25714ef0caf743a53f23530fdc61d4f08caaec2"
INSTALL_SCRIPT_SIZE = 10652
OWNER_FILE_MODE = 0o600
OWNER_DIRECTORY_MODE = 0o700
METADATA_MAX_BYTES = 256 * 1024
MANAGED_PAYLOAD_MAX_BYTES = 8 * 1024 * 1024
PROCESS_TIMEOUT_SECONDS = 120
PROCESS_OUTPUT_MAX_BYTES = 256 * 1024
SOFTWARE_DOWNLOAD_MAX_BYTES = 256 * 1024 * 1024
SOFTWARE_TREE_MAX_BYTES = 512 * 1024 * 1024
SOFTWARE_TREE_MAX_PATHS = 5000
SOFTWARE_REPLACE_PATHS = (
    Path("bin") / COMMAND_NAME,
    Path("software") / "qoder-cli.json",
)
SOFTWARE_PARENT_PATHS = tuple(
    sorted(
        {relative.parent for relative in SOFTWARE_REPLACE_PATHS if relative.parent != Path(".")},
        key=str,
    )
)
SETUP_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
MANAGED_SETTINGS_KEYS = (
    "general",
    "permissions",
    "autoMode",
    "sandbox",
    "enabledPlugins",
    "extraKnownMarketplaces",
    "hooks",
    "tools",
)
BUILDER_SOURCE_FILES = (
    (
        Path(".qoder-plugin") / "plugin.json",
        Path("plugins") / "nddev-builder" / ".qoder-plugin" / "plugin.json",
    ),
    (
        Path("skills") / "nddev-builder" / "SKILL.md",
        Path("plugins") / "nddev-builder" / "skills" / "nddev-builder" / "SKILL.md",
    ),
    (
        Path("agents") / "nddev-builder.md",
        Path("plugins") / "nddev-builder" / "agents" / "nddev-builder.md",
    ),
    (
        Path("hooks") / "hooks.json",
        Path("plugins") / "nddev-builder" / "hooks" / "hooks.json",
    ),
    (
        Path("skills") / "nddev-builder" / "SKILL.md",
        Path("skills") / "nddev-builder" / "SKILL.md",
    ),
    (Path("agents") / "nddev-builder.md", Path("agents") / "nddev-builder.md"),
    (Path("hooks") / "hooks.json", Path("hooks") / "nddev-builder.json"),
)
MANAGED_PATHS = (
    Path("settings.json"),
    Path("AGENTS.md"),
    *(target for _, target in BUILDER_SOURCE_FILES),
    Path(STAMP_NAME),
)
STAMP_KEYS = {
    "schema_version",
    "product_name",
    "build_version",
    "setup_id",
    "canonical_target",
    "managed_files",
    "builder_projection",
    "launch_args",
}
BACKUP_KEYS = {
    "schema_version",
    "product_name",
    "build_version",
    "slot",
    "canonical_target",
    "source_setup_id",
    "managed_files",
    "created_at",
}
TOKEN_ENV_NAMES = {
    "QODER_PERSONAL_ACCESS_TOKEN",
    "QODER_ACCESS_TOKEN",
    "QODER_API_KEY",
    "QODER_TOKEN",
    "SSH_ASKPASS",
    "SSH_AUTH_SOCK",
    "GIT_ASKPASS",
}
SECRET_ENV_PREFIXES = (
    "ANTHROPIC_",
    "AWS_",
    "AZURE_",
    "BUN_CONFIG_",
    "GEMINI_",
    "GOOGLE_",
    "MISTRAL_",
    "NODE_AUTH_TOKEN",
    "NPM_TOKEN",
    "OPENAI_",
    "OPENROUTER_",
    "QODER_",
    "TOGETHER_",
    "XAI_",
    "npm_config_",
)
FORBIDDEN_LAUNCH_ARGS = {
    "--accept-edits",
    "--allowed-tools",
    "--dangerously-skip-permissions",
    "--disallowed-tools",
    "--no-yolo",
    "--permission-mode",
    "--plugin-dir",
    "--settings",
    "--worktree",
    "--workspace",
    "--workspace-dir",
    "--yolo",
}
FORBIDDEN_LAUNCH_SHORT_ARGS = {"-c", "-r", "-w"}


class QoderCliSetupError(Exception):
    """A safe user-facing lifecycle failure."""


class ConcurrentTargetChange(QoderCliSetupError):
    """A fail-closed target race."""


def fail(message: str) -> NoReturn:
    raise QoderCliSetupError(message)


def fail_concurrent(message: str) -> NoReturn:
    raise ConcurrentTargetChange(message)


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def identity_of(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def owner_of(info: os.stat_result) -> int | None:
    return info.st_uid if hasattr(info, "st_uid") else None


def is_owner_only_file(info: os.stat_result) -> bool:
    if stat.S_IMODE(info.st_mode) != OWNER_FILE_MODE:
        return False
    if hasattr(os, "geteuid") and owner_of(info) != os.geteuid():
        return False
    return True


def is_owner_private_directory(info: os.stat_result) -> bool:
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != OWNER_DIRECTORY_MODE:
        return False
    if hasattr(os, "geteuid") and owner_of(info) != os.geteuid():
        return False
    return True


def require_directory(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        fail(f"{label} is missing")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        fail(f"{label} must be a real directory")
    return info


def require_regular_file(path: Path, label: str, *, owner_only: bool = False) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        fail(f"{label} is missing")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail(f"{label} must be a regular non-symlink file")
    if info.st_nlink != 1:
        fail(f"{label} must not have hard-link aliases")
    if owner_only and not is_owner_only_file(info):
        fail(f"{label} must be owned by the current user with mode 0600")
    if info.st_size > MANAGED_PAYLOAD_MAX_BYTES:
        fail(f"{label} exceeds the {MANAGED_PAYLOAD_MAX_BYTES}-byte size limit")
    return info


def require_safe_executable(path: Path, label: str) -> os.stat_result:
    info = require_regular_file(path, label)
    if hasattr(os, "geteuid") and owner_of(info) != os.geteuid():
        fail(f"{label} must be owned by the current user")
    if stat.S_IMODE(info.st_mode) != 0o700:
        fail(f"{label} must be private to the current user with mode 0700")
    return info


def path_exists_no_follow(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def reject_relative_symlink_ancestors(root: Path, relative: Path) -> None:
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            fail(f"managed parent must be a real directory: {current}")
        if not is_owner_private_directory(info):
            fail(f"managed parent must be private to the current user: {current}")


def ensure_private_parent(path: Path, label: str) -> None:
    parent = path.parent
    missing: list[Path] = []
    current = parent
    while not path_exists_no_follow(current):
        missing.append(current)
        current = current.parent
    if missing:
        info = require_directory(current, f"{label} existing ancestor")
        if not is_owner_private_directory(info):
            fail(f"{label} existing ancestor must be private to the current user with mode 0700")
        for directory in reversed(missing):
            directory.mkdir(mode=OWNER_DIRECTORY_MODE)
            directory.chmod(OWNER_DIRECTORY_MODE)
        return
    try:
        info = parent.lstat()
    except FileNotFoundError:
        fail(f"{label} parent creation failed")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        fail(f"{label} parent must be a real directory")
    if not is_owner_private_directory(info):
        fail(f"{label} parent must be private to the current user with mode 0700")


def validate_managed_destination(target: Path, relative: Path) -> None:
    if relative.is_absolute() or ".." in relative.parts:
        fail(f"unsafe managed path: {relative}")
    reject_relative_symlink_ancestors(target, relative)
    path = target / relative
    if not path_exists_no_follow(path):
        return
    require_regular_file(path, f"managed file {relative}", owner_only=True)


def validate_software_parent_destination(target: Path, relative: Path) -> None:
    path = target / relative
    if not path_exists_no_follow(path):
        return
    info = require_directory(path, f"software parent {relative}")
    if not is_owner_private_directory(info):
        fail(f"software parent {relative} must be private to the current user with mode 0700")


def validate_software_replace_destination(target: Path, relative: Path) -> None:
    destination = target / relative
    if not path_exists_no_follow(destination):
        return
    if relative == Path("bin") / COMMAND_NAME:
        require_safe_executable(destination, "Qoder CLI executable")
        return
    if relative == Path("software") / "qoder-cli.json":
        require_regular_file(destination, "software manifest", owner_only=True)
        return
    fail(f"unsupported software replace path: {relative}")


def validate_existing_software_surface(target: Path) -> None:
    for relative in SOFTWARE_PARENT_PATHS:
        validate_software_parent_destination(target, relative)
    for relative in SOFTWARE_REPLACE_PATHS:
        validate_software_replace_destination(target, relative)


def read_regular_file(
    path: Path,
    label: str,
    *,
    owner_only: bool = False,
    max_bytes: int = MANAGED_PAYLOAD_MAX_BYTES,
) -> tuple[bytes, os.stat_result]:
    before = require_regular_file(path, label, owner_only=owner_only)
    if before.st_size > max_bytes:
        fail(f"{label} exceeds the {max_bytes}-byte size limit")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if identity_of(opened) != identity_of(before):
            fail_concurrent(f"{label} changed while it was being opened")
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            fail(f"{label} changed to an unsafe file")
        if owner_only and not is_owner_only_file(opened):
            fail(f"{label} must be owned by the current user with mode 0600")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                fail(f"{label} exceeds the {max_bytes}-byte size limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    final = require_regular_file(path, label, owner_only=owner_only)
    expected = identity_of(before)
    if identity_of(after) != expected or identity_of(final) != expected:
        fail_concurrent(f"{label} changed while it was being read")
    return b"".join(chunks), final


def parse_json_object(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"cannot read valid JSON from {label}: {exc}")
    if not isinstance(value, dict):
        fail(f"{label} must contain a JSON object")
    return value


def load_json_object(path: Path, label: str, *, owner_only: bool = False) -> dict[str, Any]:
    content, _ = read_regular_file(path, label, owner_only=owner_only, max_bytes=METADATA_MAX_BYTES)
    return parse_json_object(content, label)


def require_exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        fail(
            f"{label} has invalid keys (missing={sorted(expected - actual)}, extra={sorted(actual - expected)})"
        )


def validate_setup_id(setup_id: str) -> None:
    if not SETUP_ID_PATTERN.fullmatch(setup_id):
        fail(f"invalid setup id: {setup_id!r}")


def managed_settings_view(settings: dict[str, Any]) -> dict[str, Any]:
    return {key: settings[key] for key in MANAGED_SETTINGS_KEYS if key in settings}


def merge_settings(
    existing: dict[str, Any] | None, setup_settings: dict[str, Any]
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if existing is not None:
        for key, value in existing.items():
            if key not in MANAGED_SETTINGS_KEYS:
                result[key] = value
    result.update(managed_settings_view(setup_settings))
    return result


def managed_digest(relative: Path, content: bytes) -> str:
    if relative == Path("settings.json"):
        settings = parse_json_object(content, str(relative))
        return sha256_bytes(canonical_json(managed_settings_view(settings)))
    return sha256_bytes(content)


def validate_settings(settings: dict[str, Any], label: str) -> None:
    general = settings.get("general")
    permissions = settings.get("permissions")
    sandbox = settings.get("sandbox")
    if not isinstance(general, dict) or general.get("defaultPermissionMode") not in {
        "default",
        "accept_edits",
        "auto",
        "bypass_permissions",
        "dont_ask",
    }:
        fail(f"{label} has invalid general.defaultPermissionMode")
    if general.get("enableAutoUpdate") is not False:
        fail(f"{label} must disable automatic update")
    if not isinstance(permissions, dict):
        fail(f"{label} has invalid permissions")
    for key in ("allow", "ask", "deny"):
        if key in permissions and (
            not isinstance(permissions[key], list)
            or not all(isinstance(item, str) for item in permissions[key])
        ):
            fail(f"{label} permissions.{key} must be a string array")
    if (
        not isinstance(sandbox, dict)
        or sandbox.get("gitAuth") is not False
        or sandbox.get("ghAuth") is not False
    ):
        fail(f"{label} must disable sandbox git/gh auth")
    if settings.get("enabledPlugins") != {"nddev-builder@local": True}:
        fail(f"{label} must enable nddev-builder@local")
    if settings.get("extraKnownMarketplaces") != {}:
        fail(f"{label} must not configure marketplaces")


def validate_setup_metadata(metadata: dict[str, Any], setup_id: str) -> None:
    require_exact_keys(
        metadata,
        {
            "schema_version",
            "id",
            "description",
            "managed_files",
            "builder_projection",
            "builder_default_on",
            "launch_args",
        },
        f"setup {setup_id} metadata",
    )
    if metadata["schema_version"] != 1:
        fail(f"setup {setup_id} metadata has unsupported schema")
    if metadata["id"] != setup_id:
        fail(f"setup {setup_id} metadata identity mismatch")
    if metadata["managed_files"] != ["settings.json", "AGENTS.md"]:
        fail(f"setup {setup_id} managed file declaration is invalid")
    if (
        metadata["builder_projection"] != "native-plugin-plus-user-files"
        or metadata["builder_default_on"] is not True
    ):
        fail(f"setup {setup_id} must enable native builder projection")
    if not isinstance(metadata["launch_args"], list) or not all(
        isinstance(item, str) for item in metadata["launch_args"]
    ):
        fail(f"setup {setup_id} launch_args must be a string array")


def render_builder_files() -> dict[Path, bytes]:
    files: dict[Path, bytes] = {}
    for source_relative, target_relative in BUILDER_SOURCE_FILES:
        content, _ = read_regular_file(
            BUILDER_ROOT / source_relative, f"builder source {source_relative}"
        )
        files[target_relative] = content
    return files


def build_stamp(
    setup_id: str, desired: dict[Path, bytes], launch_args: list[str]
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "setup_id": setup_id,
        "canonical_target": "",
        "managed_files": {
            str(relative): managed_digest(relative, content)
            for relative, content in desired.items()
            if relative != Path(STAMP_NAME)
        },
        "builder_projection": "qoder-native-plugin-user-files",
        "launch_args": launch_args,
    }


def bind_stamp(stamp: dict[str, Any], canonical_target: Path) -> dict[str, Any]:
    bound = dict(stamp)
    bound["canonical_target"] = str(canonical_target)
    return bound


def render_setup(
    setup_id: str, *, existing_settings: dict[str, Any] | None = None
) -> tuple[dict[str, Any], dict[Path, bytes]]:
    validate_setup_id(setup_id)
    setup_root = CATALOG_ROOT / setup_id
    if not setup_root.is_dir() or setup_root.is_symlink():
        fail(f"unknown setup: {setup_id}")
    metadata = load_json_object(setup_root / "setup.json", f"setup {setup_id} metadata")
    validate_setup_metadata(metadata, setup_id)
    settings = load_json_object(setup_root / "settings.json", f"setup {setup_id}/settings.json")
    validate_settings(settings, f"setup {setup_id}/settings.json")
    agents_md, _ = read_regular_file(setup_root / "AGENTS.md", f"setup {setup_id}/AGENTS.md")
    desired: dict[Path, bytes] = {
        Path("settings.json"): canonical_json(merge_settings(existing_settings, settings)),
        Path("AGENTS.md"): agents_md,
    }
    desired.update(render_builder_files())
    desired[Path(STAMP_NAME)] = canonical_json(
        build_stamp(setup_id, desired, metadata["launch_args"])
    )
    return metadata, desired


def list_setups() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in sorted(CATALOG_ROOT.iterdir()):
        if not path.is_dir() or path.is_symlink():
            continue
        metadata = load_json_object(path / "setup.json", f"setup {path.name} metadata")
        validate_setup_metadata(metadata, path.name)
        result.append(
            {
                "id": metadata["id"],
                "description": metadata["description"],
                "builder_default_on": metadata["builder_default_on"],
                "launch_args": metadata["launch_args"],
            }
        )
    return result


def backup_pool(target: Path) -> Path:
    return target.parent / f".{target.name}.nddev-qoder-cli-backups"


def lock_path(target: Path) -> Path:
    return target.parent / f".{target.name}.nddev-qoder-cli.lock"


@contextlib.contextmanager
def target_lock(target: Path) -> Iterator[None]:
    path = lock_path(target)
    require_directory(path.parent, "target parent")
    try:
        os.mkdir(path, OWNER_DIRECTORY_MODE)
        os.chmod(path, OWNER_DIRECTORY_MODE)
    except FileExistsError:
        fail(f"target is locked: {path}")
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            path.rmdir()


def require_explicit_absolute_target(raw_target: str | None) -> Path:
    if not raw_target:
        fail("an explicit --target absolute path is required")
    target = Path(raw_target)
    if not target.is_absolute():
        fail("--target must be an absolute path")
    try:
        info = target.lstat()
    except FileNotFoundError:
        return target
    if stat.S_ISLNK(info.st_mode):
        fail("--target must not be a symlink")
    if not stat.S_ISDIR(info.st_mode):
        fail("--target must be a directory")
    return target.resolve()


def ensure_target_directory(target: Path) -> Path:
    if target.exists():
        info = require_directory(target, "target")
        if not is_owner_private_directory(info):
            fail("target must be private to the current user with mode 0700")
        return target.resolve()
    require_directory(target.parent, "target parent")
    target.mkdir(mode=OWNER_DIRECTORY_MODE)
    target.chmod(OWNER_DIRECTORY_MODE)
    return target.resolve()


def any_managed_path_exists(target: Path) -> bool:
    return any(
        (target / relative).exists() or (target / relative).is_symlink()
        for relative in MANAGED_PATHS
    )


def load_stamp(target: Path) -> dict[str, Any] | None:
    stamp = target / STAMP_NAME
    if not stamp.exists() and not stamp.is_symlink():
        return None
    value = load_json_object(stamp, "setup stamp", owner_only=True)
    require_exact_keys(value, STAMP_KEYS, "setup stamp")
    if (
        value["schema_version"] != 1
        or value["product_name"] != PRODUCT_NAME
        or value["build_version"] != VERSION
    ):
        fail("setup stamp is not compatible with this build")
    if value["canonical_target"] != str(target):
        fail("setup stamp is bound to a different canonical target")
    if not isinstance(value["managed_files"], dict):
        fail("setup stamp managed_files must be an object")
    validate_setup_id(value["setup_id"])
    return value


def validate_managed_files(target: Path, stamp: dict[str, Any]) -> list[str]:
    expected = stamp["managed_files"]
    ordered = [relative for relative in MANAGED_PATHS if str(relative) in expected]
    ordered.extend(Path(raw) for raw in sorted(set(expected) - {str(item) for item in ordered}))
    drift: list[str] = []
    for relative in ordered:
        if relative.is_absolute() or ".." in relative.parts:
            fail("setup stamp contains an unsafe managed path")
        content, _ = read_regular_file(
            target / relative, f"managed file {relative}", owner_only=True
        )
        if managed_digest(relative, content) != expected[str(relative)]:
            drift.append(str(relative))
    if drift:
        fail(f"managed target drift detected: {', '.join(sorted(drift))}")
    return sorted(expected)


def inspect_target(target: Path) -> dict[str, Any]:
    if not target.exists():
        return {"state": "missing", "target": str(target)}
    info = require_directory(target, "target")
    if not is_owner_private_directory(info):
        fail("target must be private to the current user with mode 0700")
    stamp = load_stamp(target)
    if stamp is None:
        if any_managed_path_exists(target):
            fail("unmanaged target contains nddev-managed paths")
        return {"state": "unmanaged", "target": str(target)}
    return {
        "state": "managed",
        "target": str(target),
        "setup_id": stamp["setup_id"],
        "build_version": stamp["build_version"],
        "managed_files": validate_managed_files(target, stamp),
        "builder_projection": stamp["builder_projection"],
        "launch_args": stamp["launch_args"],
    }


def read_existing_settings_if_managed(target: Path, state: dict[str, Any]) -> dict[str, Any] | None:
    if state.get("state") != "managed":
        return None
    return load_json_object(target / "settings.json", "existing settings.json", owner_only=True)


def current_managed_snapshot(target: Path) -> dict[Path, bytes | None]:
    snapshot: dict[Path, bytes | None] = {}
    for relative in MANAGED_PATHS:
        validate_managed_destination(target, relative)
        path = target / relative
        if path_exists_no_follow(path):
            content, _ = read_regular_file(path, f"managed file {relative}", owner_only=True)
            snapshot[relative] = content
        else:
            snapshot[relative] = None
    return snapshot


def prune_empty_managed_dirs(target: Path) -> None:
    candidates = sorted(
        {(target / relative).parent for relative in MANAGED_PATHS},
        key=lambda item: len(item.parts),
        reverse=True,
    )
    for directory in candidates:
        while directory != target and directory.is_dir():
            try:
                directory.rmdir()
            except OSError:
                break
            directory = directory.parent


def restore_snapshot(target: Path, snapshot: dict[Path, bytes | None]) -> None:
    for relative in sorted(MANAGED_PATHS, key=lambda item: len(item.parts), reverse=True):
        validate_managed_destination(target, relative)
        path = target / relative
        if path_exists_no_follow(path):
            path.unlink()
    for relative, content in snapshot.items():
        if content is None:
            continue
        path = target / relative
        ensure_private_parent(path, f"managed file {relative}")
        path.write_bytes(content)
        path.chmod(OWNER_FILE_MODE)
    prune_empty_managed_dirs(target)


def replace_managed_state(
    target: Path, desired: dict[Path, bytes | None], expected: dict[str, Any]
) -> None:
    del expected
    for relative, content in desired.items():
        validate_managed_destination(target, relative)
        path = target / relative
        if content is None:
            if path_exists_no_follow(path):
                require_regular_file(path, f"managed file {relative}", owner_only=True)
                path.unlink()
            continue
        ensure_private_parent(path, f"managed file {relative}")
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        temporary.chmod(OWNER_FILE_MODE)
        os.replace(temporary, path)
        path.chmod(OWNER_FILE_MODE)
    prune_empty_managed_dirs(target)


def changed_paths(target: Path, desired: dict[Path, bytes | None]) -> list[str]:
    changed: list[str] = []
    for relative, content in desired.items():
        validate_managed_destination(target, relative)
        path = target / relative
        if content is None:
            if path_exists_no_follow(path):
                changed.append(str(relative))
            continue
        if not path_exists_no_follow(path):
            changed.append(str(relative))
            continue
        actual, _ = read_regular_file(path, f"managed file {relative}", owner_only=True)
        if actual != content:
            changed.append(str(relative))
    return sorted(changed)


def create_backup(target: Path, state: dict[str, Any]) -> int:
    pool = backup_pool(target)
    pool.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
    for slot in range(9, -1, -1):
        current = pool / str(slot)
        if not current.exists():
            continue
        if slot == 9:
            shutil.rmtree(current)
        else:
            os.replace(current, pool / str(slot + 1))
    slot_dir = pool / "0"
    slot_dir.mkdir(mode=OWNER_DIRECTORY_MODE)
    managed_files = list(state["managed_files"])
    for raw_relative in [*managed_files, STAMP_NAME]:
        relative = Path(raw_relative)
        content, _ = read_regular_file(
            target / relative, f"managed file {relative}", owner_only=True
        )
        destination = slot_dir / relative
        destination.parent.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
        destination.write_bytes(content)
        destination.chmod(OWNER_FILE_MODE)
    envelope = {
        "schema_version": 1,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "slot": 0,
        "canonical_target": str(target),
        "source_setup_id": state["setup_id"],
        "managed_files": managed_files,
        "created_at": int(time.time()),
    }
    (slot_dir / BACKUP_NAME).write_bytes(canonical_json(envelope))
    (slot_dir / BACKUP_NAME).chmod(OWNER_FILE_MODE)
    refresh_backup_slot_numbers(pool)
    return 0


def refresh_backup_slot_numbers(pool: Path) -> None:
    for slot in range(10):
        envelope_path = pool / str(slot) / BACKUP_NAME
        if not envelope_path.exists():
            continue
        envelope = load_json_object(envelope_path, f"backup slot {slot} envelope", owner_only=True)
        envelope["slot"] = slot
        envelope_path.write_bytes(canonical_json(envelope))
        envelope_path.chmod(OWNER_FILE_MODE)


def load_backup(target: Path, slot: int) -> tuple[dict[str, Any], dict[Path, bytes]]:
    if slot < 0 or slot > 9:
        fail("--backup must be between 0 and 9")
    slot_dir = backup_pool(target) / str(slot)
    require_directory(slot_dir, f"backup slot {slot}")
    envelope = load_json_object(slot_dir / BACKUP_NAME, "backup envelope", owner_only=True)
    require_exact_keys(envelope, BACKUP_KEYS, "backup envelope")
    if (
        envelope["schema_version"] != 1
        or envelope["product_name"] != PRODUCT_NAME
        or envelope["build_version"] != VERSION
    ):
        fail("backup envelope is not compatible with this build")
    if envelope["canonical_target"] != str(target):
        fail("backup belongs to a different canonical target")
    files: dict[Path, bytes] = {}
    for raw_relative in [*envelope["managed_files"], STAMP_NAME]:
        relative = Path(raw_relative)
        content, _ = read_regular_file(
            slot_dir / relative, f"backup file {relative}", owner_only=True
        )
        files[relative] = content
    return envelope, files


def mutate_setup(target: Path, setup_id: str, operation: str) -> dict[str, Any]:
    canonical_target = ensure_target_directory(target)
    with target_lock(canonical_target):
        state = inspect_target(canonical_target)
        existing_settings = read_existing_settings_if_managed(canonical_target, state)
        metadata, desired = render_setup(setup_id, existing_settings=existing_settings)
        stamp = bind_stamp(
            parse_json_object(desired[Path(STAMP_NAME)], "desired stamp"), canonical_target
        )
        desired[Path(STAMP_NAME)] = canonical_json(stamp)
        changed = changed_paths(canonical_target, desired)
        backup_slot: int | None = None
        snapshot = current_managed_snapshot(canonical_target)
        try:
            if state["state"] == "managed" and changed:
                backup_slot = create_backup(canonical_target, state)
            if changed:
                replace_managed_state(canonical_target, desired, stamp)
            post = inspect_target(canonical_target)
        except BaseException:
            restore_snapshot(canonical_target, snapshot)
            raise
    return {
        "ok": True,
        "operation": operation,
        "setup_id": setup_id,
        "description": metadata["description"],
        "target": str(canonical_target),
        "changed": changed,
        "backup_slot": backup_slot,
        "state": post["state"],
    }


def plan_setup(target: Path, setup_id: str) -> dict[str, Any]:
    canonical_target = target.resolve() if target.exists() else target
    state = inspect_target(canonical_target)
    existing_settings = read_existing_settings_if_managed(canonical_target, state)
    _metadata, desired = render_setup(setup_id, existing_settings=existing_settings)
    if state["state"] == "managed":
        stamp = bind_stamp(
            parse_json_object(desired[Path(STAMP_NAME)], "desired stamp"), canonical_target
        )
        desired[Path(STAMP_NAME)] = canonical_json(stamp)
        changed = changed_paths(canonical_target, desired)
        operation = "switch" if state.get("setup_id") != setup_id else "install"
        backup_required = bool(changed)
    else:
        changed = sorted(str(path) for path in desired)
        operation = "install"
        backup_required = False
    return {
        "ok": True,
        "operation": operation,
        "setup_id": setup_id,
        "target": str(canonical_target),
        "state": state["state"],
        "mutates": False,
        "backup_required": backup_required,
        "changed": changed,
    }


def remove_setup(target: Path) -> dict[str, Any]:
    canonical_target = require_explicit_absolute_target(str(target))
    with target_lock(canonical_target):
        state = inspect_target(canonical_target)
        if state["state"] != "managed":
            fail("target is not managed by nddev-qoder-cli-app")
        snapshot = current_managed_snapshot(canonical_target)
        desired = {relative: None for relative in MANAGED_PATHS}
        try:
            backup_slot = create_backup(canonical_target, state)
            replace_managed_state(canonical_target, desired, {})
        except BaseException:
            restore_snapshot(canonical_target, snapshot)
            raise
    return {
        "ok": True,
        "operation": "remove",
        "target": str(canonical_target),
        "removed_setup_id": state["setup_id"],
        "backup_slot": backup_slot,
    }


def restore_backup(target: Path, slot: int) -> dict[str, Any]:
    canonical_target = require_explicit_absolute_target(str(target))
    with target_lock(canonical_target):
        state = inspect_target(canonical_target)
        if state["state"] != "managed":
            fail("target is not managed by nddev-qoder-cli-app")
        envelope, desired = load_backup(canonical_target, slot)
        snapshot = current_managed_snapshot(canonical_target)
        try:
            replace_managed_state(canonical_target, desired, {})
            post = inspect_target(canonical_target)
        except BaseException:
            restore_snapshot(canonical_target, snapshot)
            raise
    return {
        "ok": True,
        "operation": "restore",
        "target": str(canonical_target),
        "setup_id": post["setup_id"],
        "restored_from_slot": slot,
        "restored_source_setup_id": envelope["source_setup_id"],
    }


def load_baseline() -> dict[str, Any]:
    return load_json_object(BASELINE_REF, "Qoder CLI baseline")


def detect_platform_asset() -> tuple[str, dict[str, Any]]:
    baseline = load_baseline()
    files = baseline.get("manifest", {}).get("files")
    if not isinstance(files, dict):
        fail("baseline manifest files missing")
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        arch = "amd64"
    elif machine in {"arm64", "aarch64"}:
        arch = "arm64"
    else:
        fail(f"unsupported architecture for Qoder CLI install: {platform.machine()}")
    if sys.platform == "darwin":
        key = f"darwin-{arch}"
    elif sys.platform.startswith("linux"):
        key = f"linux-{arch}"
    elif sys.platform.startswith("win"):
        key = "windows-amd64"
    else:
        fail(f"unsupported platform for Qoder CLI install: {sys.platform}")
    asset = files.get(key)
    if not isinstance(asset, dict):
        fail(f"baseline does not declare asset {key}")
    return key, asset


def software_manifest_path(target: Path) -> Path:
    return target / "software" / "qoder-cli.json"


def qoder_executable(target: Path) -> Path:
    suffix = ".exe" if sys.platform.startswith("win") else ""
    return target / "bin" / f"{COMMAND_NAME}{suffix}"


def software_presence(target: Path) -> dict[str, Any]:
    replace_paths_present = [
        str(relative)
        for relative in SOFTWARE_REPLACE_PATHS
        if path_exists_no_follow(target / relative)
    ]
    owned_parent_paths_present = [
        str(relative)
        for relative in SOFTWARE_PARENT_PATHS
        if path_exists_no_follow(target / relative)
    ]
    if not replace_paths_present and not owned_parent_paths_present:
        state = "absent"
    elif len(replace_paths_present) == len(SOFTWARE_REPLACE_PATHS):
        state = "installed"
    else:
        state = "partial"
    return {
        "software_state": state,
        "partial": state == "partial",
        "replace_paths_present": replace_paths_present,
        "owned_parent_paths_present": owned_parent_paths_present,
    }


def file_sha256(
    path: Path, label: str, *, max_bytes: int = SOFTWARE_TREE_MAX_BYTES
) -> tuple[str, int]:
    info = require_regular_file(path, label)
    if info.st_size > max_bytes:
        fail(f"{label} exceeds the {max_bytes}-byte size limit")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    total = 0
    try:
        opened = os.fstat(descriptor)
        if identity_of(opened) != identity_of(info):
            fail_concurrent(f"{label} changed while it was opened")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                fail(f"{label} exceeds the {max_bytes}-byte size limit")
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    final = require_regular_file(path, label)
    expected = identity_of(info)
    if (
        identity_of(opened) != expected
        or identity_of(after) != expected
        or identity_of(final) != expected
    ):
        fail_concurrent(f"{label} changed while it was read")
    return digest.hexdigest(), total


def expected_software_identity(asset_key: str, asset: dict[str, Any]) -> dict[str, Any]:
    size = asset.get("size")
    if not isinstance(size, int) or size <= 0:
        fail(f"baseline asset {asset_key} is missing exact size")
    url = asset.get("url")
    digest = asset.get("sha256")
    if not isinstance(url, str) or not isinstance(digest, str):
        fail(f"baseline asset {asset_key} is incomplete")
    return {
        "schema_version": 1,
        "version": TESTED_VERSION,
        "executable": f"bin/{COMMAND_NAME}",
        "asset": asset_key,
        "asset_size": size,
        "asset_sha256": digest,
        "source": url,
        "installer": {
            "source": "official-release-asset",
            "reason": (
                "The official shell installer verifies the same manifest asset but delegates to "
                "the binary install subcommand for PATH and marker mutation; this manager uses "
                "the verified release asset directly to keep installation target-owned."
            ),
        },
    }


def build_software_manifest(
    root: Path,
    *,
    asset_key: str,
    asset: dict[str, Any],
) -> dict[str, Any]:
    executable = qoder_executable(root)
    executable_sha256, executable_size = file_sha256(
        executable, "Qoder CLI executable", max_bytes=SOFTWARE_TREE_MAX_BYTES
    )
    return {
        **expected_software_identity(asset_key, asset),
        "executable_sha256": executable_sha256,
        "executable_size": executable_size,
    }


def software_status(target: Path) -> dict[str, Any]:
    if not path_exists_no_follow(target):
        return {
            "ok": True,
            "installed": False,
            "current": False,
            "target": str(target),
            "version": None,
            "executable": None,
            "software_state": "absent",
            "partial": False,
            "replace_paths_present": [],
            "owned_parent_paths_present": [],
        }
    canonical_target = require_explicit_absolute_target(str(target))
    target_info = require_directory(canonical_target, "software target")
    if not is_owner_private_directory(target_info):
        fail("software target must be private to the current user with mode 0700")
    presence = software_presence(canonical_target)
    executable = qoder_executable(canonical_target)
    manifest = software_manifest_path(canonical_target)
    if presence["software_state"] != "installed":
        return {
            "ok": True,
            "installed": False,
            "current": False,
            "target": str(canonical_target),
            "version": None,
            "executable": str(executable),
            **presence,
        }
    try:
        validate_existing_software_surface(canonical_target)
        info = load_json_object(manifest, "software manifest", owner_only=True)
        asset_key = str(info.get("asset", ""))
        baseline = load_baseline()
        asset = baseline.get("manifest", {}).get("files", {}).get(asset_key)
        if not isinstance(asset, dict):
            fail("software manifest references an unknown Qoder CLI asset")
        expected = build_software_manifest(canonical_target, asset_key=asset_key, asset=asset)
        current = info == expected
        validation_error = None
    except QoderCliSetupError as exc:
        info = {}
        current = False
        validation_error = str(exc)
    return {
        "ok": True,
        "installed": True,
        "current": current,
        "target": str(canonical_target),
        "version": info.get("version"),
        "executable": str(executable),
        **presence,
        **({"validation_error": validation_error} if validation_error else {}),
    }


def download_asset_bytes(url: str, *, expected_size: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": PRODUCT_NAME})
    try:
        response_context = urllib.request.urlopen(request, timeout=PROCESS_TIMEOUT_SECONDS)
    except TimeoutError:
        fail(f"Qoder CLI download timed out: {url}")
    except OSError as exc:
        fail(f"Qoder CLI download failed: {exc}")
    with response_context as response:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                fail("Qoder CLI download returned invalid Content-Length")
            if declared != expected_size:
                fail(
                    f"Qoder CLI download Content-Length mismatch: expected {expected_size}, got {declared}"
                )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > SOFTWARE_DOWNLOAD_MAX_BYTES:
                fail(f"Qoder CLI download exceeded the {SOFTWARE_DOWNLOAD_MAX_BYTES}-byte bound")
            chunks.append(chunk)
    if total != expected_size:
        fail(f"Qoder CLI download size mismatch: expected {expected_size}, got {total}")
    return b"".join(chunks)


def validate_archive_member_path(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    if "\x00" in normalized:
        fail(f"unsafe archive member path: {name}")
    if normalized.startswith("//"):
        fail(f"unsafe archive member path: {name}")
    if re.fullmatch(r"(?i)[a-z]:.*", normalized):
        fail(f"unsafe archive member path: {name}")
    path = PurePosixPath(normalized)
    if not path.parts or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        fail(f"unsafe archive member path: {name}")
    return path


def safe_tar_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    if len(members) > SOFTWARE_TREE_MAX_PATHS:
        fail(f"Qoder CLI archive exceeds the {SOFTWARE_TREE_MAX_PATHS}-path limit")
    seen: set[str] = set()
    total_size = 0
    for member in members:
        path = validate_archive_member_path(member.name)
        path_key = path.as_posix()
        if path_key in seen:
            fail(f"duplicate archive member path: {member.name}")
        seen.add(path_key)
        if member.issym() or member.islnk() or member.isdev():
            fail(f"unsafe archive member type: {member.name}")
        if member.isfile():
            total_size += member.size
            if total_size > SOFTWARE_TREE_MAX_BYTES:
                fail(f"Qoder CLI archive exceeds the {SOFTWARE_TREE_MAX_BYTES}-byte limit")
    return members


def safe_zip_infos(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > SOFTWARE_TREE_MAX_PATHS:
        fail(f"Qoder CLI archive exceeds the {SOFTWARE_TREE_MAX_PATHS}-path limit")
    seen: set[str] = set()
    total_size = 0
    for info in infos:
        path = validate_archive_member_path(info.filename)
        path_key = path.as_posix()
        if path_key in seen:
            fail(f"duplicate archive member path: {info.filename}")
        seen.add(path_key)
        file_type = (info.external_attr >> 16) & 0o170000
        if info.is_dir():
            if file_type and not stat.S_ISDIR(file_type):
                fail(f"unsafe archive member type: {info.filename}")
            continue
        if file_type and not stat.S_ISREG(file_type):
            fail(f"unsafe archive member type: {info.filename}")
        total_size += info.file_size
        if total_size > SOFTWARE_TREE_MAX_BYTES:
            fail(f"Qoder CLI archive exceeds the {SOFTWARE_TREE_MAX_BYTES}-byte limit")
    return infos


def chmod_private_tree(root: Path) -> None:
    total_bytes = 0
    total_paths = 0
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts)):
        total_paths += 1
        if total_paths > SOFTWARE_TREE_MAX_PATHS:
            fail(f"Qoder CLI archive exceeds the {SOFTWARE_TREE_MAX_PATHS}-path limit")
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            fail(f"Qoder CLI archive contains symlink: {path.relative_to(root)}")
        if stat.S_ISDIR(info.st_mode):
            path.chmod(OWNER_DIRECTORY_MODE)
            continue
        if info.st_nlink != 1:
            fail(f"Qoder CLI archive contains hard-link alias: {path.relative_to(root)}")
        if not stat.S_ISREG(info.st_mode):
            fail(f"Qoder CLI archive contains unsupported file type: {path.relative_to(root)}")
        total_bytes += info.st_size
        if total_bytes > SOFTWARE_TREE_MAX_BYTES:
            fail(f"Qoder CLI archive exceeds the {SOFTWARE_TREE_MAX_BYTES}-byte limit")
        path.chmod(0o700 if stat.S_IMODE(info.st_mode) & stat.S_IXUSR else OWNER_FILE_MODE)


def bounded_process(
    command: list[str], *, cwd: Path, env: dict[str, str], timeout: int
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            start_new_session=True,
        )
    except FileNotFoundError:
        fail(f"process executable not found: {command[0]}")
    except subprocess.TimeoutExpired:
        fail(f"process timed out after {timeout} seconds: {command[0]}")
    if (
        len(completed.stdout) > PROCESS_OUTPUT_MAX_BYTES
        or len(completed.stderr) > PROCESS_OUTPUT_MAX_BYTES
    ):
        fail(f"process output exceeded the {PROCESS_OUTPUT_MAX_BYTES}-byte limit: {command[0]}")
    return completed


def stage_probe_environment(stage: Path) -> dict[str, str]:
    home = stage / "probe-home"
    tmp = stage / "probe-tmp"
    for directory in (home, tmp):
        directory.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
        directory.chmod(OWNER_DIRECTORY_MODE)
    return {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "QODER_HOME": str(home),
        "QODER_AUTO_UPDATE": "false",
        "TMPDIR": str(tmp),
    }


def probe_staged_binary(executable: Path, stage: Path) -> None:
    require_safe_executable(executable, "staged Qoder CLI executable")
    completed = bounded_process(
        [str(executable), "--version"],
        cwd=stage,
        env=stage_probe_environment(stage),
        timeout=PROCESS_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        fail(f"staged Qoder CLI version probe failed with exit {completed.returncode}")
    output = "\n".join((completed.stdout, completed.stderr))
    if TESTED_VERSION not in output:
        fail(f"staged Qoder CLI reported an unexpected version: {output.strip()!r}")


def extract_archive(archive_path: Path, unpacked: Path) -> None:
    unpacked.mkdir(mode=OWNER_DIRECTORY_MODE)
    if archive_path.name.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as archive:
            for info in safe_zip_infos(archive):
                member_path = validate_archive_member_path(info.filename)
                destination = unpacked / Path(member_path.as_posix())
                if info.is_dir():
                    destination.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
                    continue
                ensure_private_parent(destination, f"archive member {info.filename}")
                with archive.open(info) as source, destination.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
                member_mode = (info.external_attr >> 16) & 0o777
                destination.chmod(0o700 if member_mode & stat.S_IXUSR else OWNER_FILE_MODE)
    else:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in safe_tar_members(archive):
                member_path = validate_archive_member_path(member.name)
                destination = unpacked / Path(member_path.as_posix())
                if member.isdir():
                    destination.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    fail(f"unsafe archive member type: {member.name}")
                ensure_private_parent(destination, f"archive member {member.name}")
                source = archive.extractfile(member)
                if source is None:
                    fail(f"cannot extract archive member: {member.name}")
                with source, destination.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
                destination.chmod(0o700 if member.mode & stat.S_IXUSR else OWNER_FILE_MODE)
    chmod_private_tree(unpacked)


def find_extracted_binary(unpacked: Path) -> Path:
    candidates: list[Path] = []
    for candidate in sorted(unpacked.rglob("*")):
        if candidate.name in {COMMAND_NAME, f"{COMMAND_NAME}.exe"} and candidate.is_file():
            require_safe_executable(candidate, "extracted Qoder CLI executable")
            candidates.append(candidate)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        fail("Qoder CLI archive contained multiple qodercli executables")
    fail("Qoder CLI archive did not contain a qodercli executable")


def build_live_stage(staging: Path, asset_key: str, asset: dict[str, Any]) -> Path:
    url = asset.get("url")
    expected_sha = asset.get("sha256")
    expected_size = asset.get("size")
    if (
        not isinstance(url, str)
        or not isinstance(expected_sha, str)
        or not isinstance(expected_size, int)
    ):
        fail(f"baseline asset {asset_key} is incomplete")
    archive_bytes = download_asset_bytes(url, expected_size=expected_size)
    actual_sha = sha256_bytes(archive_bytes)
    if actual_sha != expected_sha:
        fail(f"downloaded Qoder CLI digest mismatch for {asset_key}")
    archive_path = staging / Path(url).name
    archive_path.write_bytes(archive_bytes)
    archive_path.chmod(OWNER_FILE_MODE)
    unpacked = staging / "unpacked"
    extract_archive(archive_path, unpacked)
    binary_source = find_extracted_binary(unpacked)
    live = staging / "live"
    (live / "bin").mkdir(mode=OWNER_DIRECTORY_MODE, parents=True)
    (live / "software").mkdir(mode=OWNER_DIRECTORY_MODE, parents=True)
    staged_executable = live / "bin" / COMMAND_NAME
    shutil.copy2(binary_source, staged_executable)
    staged_executable.chmod(0o700)
    probe_staged_binary(staged_executable, staging)
    manifest = build_software_manifest(live, asset_key=asset_key, asset=asset)
    software_manifest_path(live).write_bytes(canonical_json(manifest))
    software_manifest_path(live).chmod(OWNER_FILE_MODE)
    return live


def cleanup_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def move_replace_path(source: Path, destination: Path) -> None:
    ensure_private_parent(destination, f"software destination {destination}")
    os.replace(source, destination)


def restore_software_paths(
    target: Path,
    hold: Path,
    *,
    moved_old: list[Path],
    installed_new: list[Path],
    preexisting_parent_paths: set[Path],
) -> None:
    for relative in reversed(installed_new):
        destination = target / relative
        if path_exists_no_follow(destination):
            cleanup_path(destination)
    for relative in reversed(moved_old):
        saved = hold / relative
        if path_exists_no_follow(saved):
            move_replace_path(saved, target / relative)
    for relative in sorted(SOFTWARE_PARENT_PATHS, key=lambda item: len(item.parts), reverse=True):
        if relative in preexisting_parent_paths:
            continue
        parent = target / relative
        if not parent.exists() or parent.is_symlink() or not parent.is_dir():
            continue
        with contextlib.suppress(OSError):
            parent.rmdir()


def replace_software_state(target: Path, live_stage: Path) -> None:
    for relative in SOFTWARE_REPLACE_PATHS:
        validate_software_replace_destination(live_stage, relative)
        validate_software_replace_destination(target, relative)
    hold = Path(tempfile.mkdtemp(dir=target.parent, prefix=f".{target.name}.nddev-qoder-cli-hold."))
    hold.chmod(OWNER_DIRECTORY_MODE)
    preexisting_parent_paths = {
        relative for relative in SOFTWARE_PARENT_PATHS if path_exists_no_follow(target / relative)
    }
    moved_old: list[Path] = []
    installed_new: list[Path] = []
    try:
        for relative in SOFTWARE_REPLACE_PATHS:
            destination = target / relative
            if path_exists_no_follow(destination):
                saved = hold / relative
                ensure_private_parent(saved, f"rollback path {saved}")
                os.replace(destination, saved)
                moved_old.append(relative)
        for relative in SOFTWARE_REPLACE_PATHS:
            move_replace_path(live_stage / relative, target / relative)
            installed_new.append(relative)
        status = software_status(target)
        if not status["installed"] or not status["current"]:
            detail = f": {status['validation_error']}" if "validation_error" in status else ""
            fail(f"installed Qoder CLI failed validation{detail}")
    except BaseException:
        restore_software_paths(
            target,
            hold,
            moved_old=moved_old,
            installed_new=installed_new,
            preexisting_parent_paths=preexisting_parent_paths,
        )
        raise
    finally:
        shutil.rmtree(hold, ignore_errors=True)


def remove_created_target_if_empty(target: Path, existed_before: bool) -> None:
    if existed_before:
        return
    try:
        if target.exists() and target.is_dir() and not any(target.iterdir()):
            target.rmdir()
    except OSError:
        pass


def install_or_update_cli(target: Path, *, operation: str) -> dict[str, Any]:
    existed_before = path_exists_no_follow(target)
    preflight = software_status(target)
    if operation == "install-cli":
        if preflight.get("replace_paths_present") or preflight.get("owned_parent_paths_present"):
            validate_existing_software_surface(require_explicit_absolute_target(str(target)))
            fail("Qoder CLI software already exists at target; use update-cli")
    elif operation == "update-cli":
        if preflight["software_state"] == "absent":
            fail("Qoder CLI is not installed at the selected target; use install-cli")
        validate_existing_software_surface(require_explicit_absolute_target(str(target)))
        if preflight["installed"] and preflight["current"]:
            return {
                "ok": True,
                "operation": operation,
                "target": str(require_explicit_absolute_target(str(target))),
                "changed": False,
                "version": preflight["version"],
                "executable": preflight["executable"],
            }
    else:
        fail(f"unsupported software operation: {operation}")
    canonical_target = target.resolve() if path_exists_no_follow(target) else target
    with target_lock(canonical_target):
        asset_key, asset = detect_platform_asset()
        staging = Path(
            tempfile.mkdtemp(dir=target.parent, prefix=f".{target.name}.nddev-qoder-cli-stage.")
        )
        staging.chmod(OWNER_DIRECTORY_MODE)
        try:
            live_stage = build_live_stage(staging, asset_key, asset)
            canonical_target = ensure_target_directory(target)
            status = software_status(canonical_target)
            if operation == "install-cli" and (
                status.get("replace_paths_present") or status.get("owned_parent_paths_present")
            ):
                fail("Qoder CLI software already exists at target; use update-cli")
            if operation == "update-cli":
                if status["software_state"] == "absent":
                    fail("Qoder CLI is not installed at the selected target; use install-cli")
                validate_existing_software_surface(canonical_target)
                if status["installed"] and status["current"]:
                    return {
                        "ok": True,
                        "operation": operation,
                        "target": str(canonical_target),
                        "changed": False,
                        "version": status["version"],
                        "executable": status["executable"],
                    }
            replace_software_state(canonical_target, live_stage)
        finally:
            remove_created_target_if_empty(target, existed_before)
            shutil.rmtree(staging, ignore_errors=True)
    status = software_status(canonical_target)
    return {
        "ok": True,
        "operation": operation,
        "target": str(canonical_target),
        "changed": True,
        "version": TESTED_VERSION,
        "asset": asset_key,
        "executable": status["executable"],
    }


def isolated_child_environment(target: Path) -> dict[str, str]:
    home = target / "home"
    runtime = target / "runtime"
    tmp = runtime / "tmp"
    for directory in (home, runtime, tmp):
        directory.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
        directory.chmod(OWNER_DIRECTORY_MODE)
    env: dict[str, str] = {}
    for name in ("LANG", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM", "NO_COLOR", "CI"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "QODER_HOME": str(target),
            "QODER_AUTO_UPDATE": "false",
            "TMPDIR": str(tmp),
            "XDG_CONFIG_HOME": str(runtime / "xdg-config"),
            "XDG_CACHE_HOME": str(runtime / "xdg-cache"),
            "XDG_STATE_HOME": str(runtime / "xdg-state"),
        }
    )
    return env


def forbidden_launch_option(argument: str) -> str | None:
    if argument == "--":
        return None
    option = argument.split("=", 1)[0]
    if option in FORBIDDEN_LAUNCH_ARGS:
        return option
    if option.startswith("--no-") and f"--{option[5:]}" in FORBIDDEN_LAUNCH_ARGS:
        return option
    if option.startswith("-") and not option.startswith("--"):
        forbidden_shorts = {short[1:] for short in FORBIDDEN_LAUNCH_SHORT_ARGS}
        for character in option[1:]:
            if character in forbidden_shorts:
                return f"-{character}"
    return None


def normalize_launch_arguments(args: list[str]) -> list[str]:
    forwarded = list(args)
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]
    for argument in forwarded:
        option = forbidden_launch_option(argument)
        if option is not None:
            fail(
                "launch argument is managed by nddev-qoder-cli-app and cannot be "
                f"overridden: {option}"
            )
    return forwarded


def launch_qoder(target: Path, args: list[str]) -> int:
    canonical_target = require_explicit_absolute_target(str(target))
    forwarded = normalize_launch_arguments(args)
    with target_lock(canonical_target):
        state = inspect_target(canonical_target)
        if state["state"] != "managed":
            fail("target is not managed by nddev-qoder-cli-app")
        status = software_status(canonical_target)
        if not status["installed"] or not status["current"]:
            fail("Qoder CLI is not installed at the tested version in this target")
        command = [
            str(qoder_executable(canonical_target)),
            *list(state["launch_args"]),
            "--settings",
            str(canonical_target / "settings.json"),
            *forwarded,
        ]
        env = isolated_child_environment(canonical_target)
        cwd = os.getcwd()
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=False,
            timeout=None,
            start_new_session=True,
        )
    except FileNotFoundError:
        fail("target-owned qodercli executable disappeared before launch")
    return int(completed.returncode)


def print_payload(payload: dict[str, Any], *, json_output: bool) -> None:
    del json_output
    print(json.dumps(payload, indent=2, sort_keys=True))


def add_json_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print JSON output")


def add_target_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", required=True, help="explicit absolute Qoder CLI home")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list", help="list setup variants")
    add_json_argument(list_parser)
    for name in ("status", "software-status"):
        command_parser = subparsers.add_parser(name, help=f"{name} for a target")
        add_target_argument(command_parser)
        add_json_argument(command_parser)
    for name in ("plan", "install", "switch"):
        command_parser = subparsers.add_parser(name, help=f"{name} a setup")
        command_parser.add_argument("--setup", required=True)
        add_target_argument(command_parser)
        add_json_argument(command_parser)
    restore_parser = subparsers.add_parser("restore", help="restore a target-bound backup")
    restore_parser.add_argument("--backup", type=int, required=True)
    add_target_argument(restore_parser)
    add_json_argument(restore_parser)
    remove_parser = subparsers.add_parser("remove", help="remove nddev-managed setup files")
    add_target_argument(remove_parser)
    add_json_argument(remove_parser)
    for name in ("install-cli", "update-cli"):
        command_parser = subparsers.add_parser(name, help=f"{name} exact tested Qoder CLI")
        add_target_argument(command_parser)
        add_json_argument(command_parser)
    launch_parser = subparsers.add_parser("launch", help="launch target-owned Qoder CLI")
    add_target_argument(launch_parser)
    add_json_argument(launch_parser)
    launch_parser.add_argument("qoder_args", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def error_result(message: str, *, json_output: bool) -> int:
    if json_output:
        print(json.dumps({"ok": False, "error": message}, indent=2, sort_keys=True))
    else:
        print(f"error: {message}", file=sys.stderr)
    return 2


def run(args: argparse.Namespace) -> int:
    if args.command == "list":
        print_payload({"ok": True, "setups": list_setups()}, json_output=args.json)
        return 0
    if args.command == "status":
        target = require_explicit_absolute_target(args.target)
        print_payload({"ok": True, **inspect_target(target)}, json_output=args.json)
        return 0
    if args.command == "software-status":
        target = require_explicit_absolute_target(args.target)
        print_payload(software_status(target), json_output=args.json)
        return 0
    if args.command == "plan":
        target = require_explicit_absolute_target(args.target)
        print_payload(plan_setup(target, args.setup), json_output=args.json)
        return 0
    if args.command in {"install", "switch"}:
        target = require_explicit_absolute_target(args.target)
        print_payload(mutate_setup(target, args.setup, args.command), json_output=args.json)
        return 0
    if args.command == "restore":
        target = require_explicit_absolute_target(args.target)
        print_payload(restore_backup(target, args.backup), json_output=args.json)
        return 0
    if args.command == "remove":
        target = require_explicit_absolute_target(args.target)
        print_payload(remove_setup(target), json_output=args.json)
        return 0
    if args.command == "install-cli":
        target = require_explicit_absolute_target(args.target)
        print_payload(install_or_update_cli(target, operation="install-cli"), json_output=args.json)
        return 0
    if args.command == "update-cli":
        target = require_explicit_absolute_target(args.target)
        print_payload(install_or_update_cli(target, operation="update-cli"), json_output=args.json)
        return 0
    if args.command == "launch":
        target = require_explicit_absolute_target(args.target)
        qoder_args = list(args.qoder_args)
        if qoder_args and qoder_args[0] == "--":
            qoder_args = qoder_args[1:]
        return launch_qoder(target, qoder_args)
    fail(f"unknown command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        return run(args)
    except QoderCliSetupError as exc:
        json_output = "--json" in (argv if argv is not None else sys.argv[1:])
        return error_result(str(exc), json_output=json_output)


if __name__ == "__main__":
    raise SystemExit(main())
