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
from pathlib import Path
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
OWNER_FILE_MODE = 0o600
OWNER_DIRECTORY_MODE = 0o700
METADATA_MAX_BYTES = 256 * 1024
MANAGED_PAYLOAD_MAX_BYTES = 8 * 1024 * 1024
PROCESS_TIMEOUT_SECONDS = 120
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
        fail(f"{label} has invalid keys (missing={sorted(expected - actual)}, extra={sorted(actual - expected)})")


def validate_setup_id(setup_id: str) -> None:
    if not SETUP_ID_PATTERN.fullmatch(setup_id):
        fail(f"invalid setup id: {setup_id!r}")


def managed_settings_view(settings: dict[str, Any]) -> dict[str, Any]:
    return {key: settings[key] for key in MANAGED_SETTINGS_KEYS if key in settings}


def merge_settings(existing: dict[str, Any] | None, setup_settings: dict[str, Any]) -> dict[str, Any]:
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
    if not isinstance(general, dict) or general.get("defaultPermissionMode") not in {"default", "accept_edits", "auto", "bypass_permissions", "dont_ask"}:
        fail(f"{label} has invalid general.defaultPermissionMode")
    if general.get("enableAutoUpdate") is not False:
        fail(f"{label} must disable automatic update")
    if not isinstance(permissions, dict):
        fail(f"{label} has invalid permissions")
    for key in ("allow", "ask", "deny"):
        if key in permissions and (not isinstance(permissions[key], list) or not all(isinstance(item, str) for item in permissions[key])):
            fail(f"{label} permissions.{key} must be a string array")
    if not isinstance(sandbox, dict) or sandbox.get("gitAuth") is not False or sandbox.get("ghAuth") is not False:
        fail(f"{label} must disable sandbox git/gh auth")
    if settings.get("enabledPlugins") != {"nddev-builder@local": True}:
        fail(f"{label} must enable nddev-builder@local")
    if settings.get("extraKnownMarketplaces") != {}:
        fail(f"{label} must not configure marketplaces")


def validate_setup_metadata(metadata: dict[str, Any], setup_id: str) -> None:
    require_exact_keys(metadata, {"schema_version", "id", "description", "managed_files", "builder_projection", "builder_default_on", "launch_args"}, f"setup {setup_id} metadata")
    if metadata["schema_version"] != 1:
        fail(f"setup {setup_id} metadata has unsupported schema")
    if metadata["id"] != setup_id:
        fail(f"setup {setup_id} metadata identity mismatch")
    if metadata["managed_files"] != ["settings.json", "AGENTS.md"]:
        fail(f"setup {setup_id} managed file declaration is invalid")
    if metadata["builder_projection"] != "native-plugin-plus-user-files" or metadata["builder_default_on"] is not True:
        fail(f"setup {setup_id} must enable native builder projection")
    if not isinstance(metadata["launch_args"], list) or not all(isinstance(item, str) for item in metadata["launch_args"]):
        fail(f"setup {setup_id} launch_args must be a string array")


def render_builder_files() -> dict[Path, bytes]:
    files: dict[Path, bytes] = {}
    for source_relative, target_relative in BUILDER_SOURCE_FILES:
        content, _ = read_regular_file(BUILDER_ROOT / source_relative, f"builder source {source_relative}")
        files[target_relative] = content
    return files


def build_stamp(setup_id: str, desired: dict[Path, bytes], launch_args: list[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "setup_id": setup_id,
        "canonical_target": "",
        "managed_files": {str(relative): managed_digest(relative, content) for relative, content in desired.items() if relative != Path(STAMP_NAME)},
        "builder_projection": "qoder-native-plugin-user-files",
        "launch_args": launch_args,
    }


def bind_stamp(stamp: dict[str, Any], canonical_target: Path) -> dict[str, Any]:
    bound = dict(stamp)
    bound["canonical_target"] = str(canonical_target)
    return bound


def render_setup(setup_id: str, *, existing_settings: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[Path, bytes]]:
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
    desired[Path(STAMP_NAME)] = canonical_json(build_stamp(setup_id, desired, metadata["launch_args"]))
    return metadata, desired


def list_setups() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in sorted(CATALOG_ROOT.iterdir()):
        if not path.is_dir() or path.is_symlink():
            continue
        metadata = load_json_object(path / "setup.json", f"setup {path.name} metadata")
        validate_setup_metadata(metadata, path.name)
        result.append({"id": metadata["id"], "description": metadata["description"], "builder_default_on": metadata["builder_default_on"], "launch_args": metadata["launch_args"]})
    return result


def backup_pool(target: Path) -> Path:
    return target.parent / f".{target.name}.nddev-qoder-cli-backups"


def lock_path(target: Path) -> Path:
    return target.parent / f".{target.name}.nddev-qoder-cli.lock"


@contextlib.contextmanager
def target_lock(target: Path) -> Iterator[None]:
    path = lock_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(path, OWNER_DIRECTORY_MODE)
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
    target = Path(raw_target).expanduser()
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
            os.chmod(target, OWNER_DIRECTORY_MODE)
        return target.resolve()
    require_directory(target.parent, "target parent")
    target.mkdir(mode=OWNER_DIRECTORY_MODE)
    return target.resolve()


def any_managed_path_exists(target: Path) -> bool:
    return any((target / relative).exists() or (target / relative).is_symlink() for relative in MANAGED_PATHS)


def load_stamp(target: Path) -> dict[str, Any] | None:
    stamp = target / STAMP_NAME
    if not stamp.exists() and not stamp.is_symlink():
        return None
    value = load_json_object(stamp, "setup stamp", owner_only=True)
    require_exact_keys(value, STAMP_KEYS, "setup stamp")
    if value["schema_version"] != 1 or value["product_name"] != PRODUCT_NAME or value["build_version"] != VERSION:
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
        content, _ = read_regular_file(target / relative, f"managed file {relative}", owner_only=True)
        if managed_digest(relative, content) != expected[str(relative)]:
            drift.append(str(relative))
    if drift:
        fail(f"managed target drift detected: {', '.join(sorted(drift))}")
    return sorted(expected)


def inspect_target(target: Path) -> dict[str, Any]:
    if not target.exists():
        return {"state": "missing", "target": str(target)}
    require_directory(target, "target")
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
        path = target / relative
        if path.exists() or path.is_symlink():
            content, _ = read_regular_file(path, f"managed file {relative}", owner_only=True)
            snapshot[relative] = content
        else:
            snapshot[relative] = None
    return snapshot


def prune_empty_managed_dirs(target: Path) -> None:
    candidates = sorted({(target / relative).parent for relative in MANAGED_PATHS}, key=lambda item: len(item.parts), reverse=True)
    for directory in candidates:
        while directory != target and directory.is_dir():
            try:
                directory.rmdir()
            except OSError:
                break
            directory = directory.parent


def restore_snapshot(target: Path, snapshot: dict[Path, bytes | None]) -> None:
    for relative in sorted(MANAGED_PATHS, key=lambda item: len(item.parts), reverse=True):
        path = target / relative
        if path.exists() or path.is_symlink():
            path.unlink()
    for relative, content in snapshot.items():
        if content is None:
            continue
        path = target / relative
        path.parent.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(OWNER_FILE_MODE)
    prune_empty_managed_dirs(target)


def replace_managed_state(target: Path, desired: dict[Path, bytes | None], expected: dict[str, Any]) -> None:
    del expected
    for relative, content in desired.items():
        path = target / relative
        if relative.is_absolute() or ".." in relative.parts:
            fail(f"unsafe managed path: {relative}")
        if content is None:
            if path.exists() or path.is_symlink():
                require_regular_file(path, f"managed file {relative}", owner_only=True)
                path.unlink()
            continue
        path.parent.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
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
        path = target / relative
        if content is None:
            if path.exists() or path.is_symlink():
                changed.append(str(relative))
            continue
        if not path.exists() or path.is_symlink():
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
        content, _ = read_regular_file(target / relative, f"managed file {relative}", owner_only=True)
        destination = slot_dir / relative
        destination.parent.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
        destination.write_bytes(content)
        destination.chmod(OWNER_FILE_MODE)
    envelope = {"schema_version": 1, "product_name": PRODUCT_NAME, "build_version": VERSION, "slot": 0, "canonical_target": str(target), "source_setup_id": state["setup_id"], "managed_files": managed_files, "created_at": int(time.time())}
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
    if envelope["schema_version"] != 1 or envelope["product_name"] != PRODUCT_NAME or envelope["build_version"] != VERSION:
        fail("backup envelope is not compatible with this build")
    if envelope["canonical_target"] != str(target):
        fail("backup belongs to a different canonical target")
    files: dict[Path, bytes] = {}
    for raw_relative in [*envelope["managed_files"], STAMP_NAME]:
        relative = Path(raw_relative)
        content, _ = read_regular_file(slot_dir / relative, f"backup file {relative}", owner_only=True)
        files[relative] = content
    return envelope, files


def mutate_setup(target: Path, setup_id: str, operation: str) -> dict[str, Any]:
    canonical_target = ensure_target_directory(target)
    with target_lock(canonical_target):
        state = inspect_target(canonical_target)
        existing_settings = read_existing_settings_if_managed(canonical_target, state)
        metadata, desired = render_setup(setup_id, existing_settings=existing_settings)
        stamp = bind_stamp(parse_json_object(desired[Path(STAMP_NAME)], "desired stamp"), canonical_target)
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
    return {"ok": True, "operation": operation, "setup_id": setup_id, "description": metadata["description"], "target": str(canonical_target), "changed": changed, "backup_slot": backup_slot, "state": post["state"]}


def plan_setup(target: Path, setup_id: str) -> dict[str, Any]:
    canonical_target = target.resolve() if target.exists() else target
    state = inspect_target(canonical_target)
    existing_settings = read_existing_settings_if_managed(canonical_target, state)
    _metadata, desired = render_setup(setup_id, existing_settings=existing_settings)
    if state["state"] == "managed":
        stamp = bind_stamp(parse_json_object(desired[Path(STAMP_NAME)], "desired stamp"), canonical_target)
        desired[Path(STAMP_NAME)] = canonical_json(stamp)
        changed = changed_paths(canonical_target, desired)
        operation = "switch" if state.get("setup_id") != setup_id else "install"
        backup_required = bool(changed)
    else:
        changed = sorted(str(path) for path in desired)
        operation = "install"
        backup_required = False
    return {"ok": True, "operation": operation, "setup_id": setup_id, "target": str(canonical_target), "state": state["state"], "mutates": False, "backup_required": backup_required, "changed": changed}


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
    return {"ok": True, "operation": "remove", "target": str(canonical_target), "removed_setup_id": state["setup_id"], "backup_slot": backup_slot}


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
    return {"ok": True, "operation": "restore", "target": str(canonical_target), "setup_id": post["setup_id"], "restored_from_slot": slot, "restored_source_setup_id": envelope["source_setup_id"]}


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


def software_status(target: Path) -> dict[str, Any]:
    if not target.exists():
        return {"ok": True, "installed": False, "current": False, "target": str(target), "version": None, "executable": None}
    canonical_target = require_explicit_absolute_target(str(target))
    executable = qoder_executable(canonical_target)
    manifest = software_manifest_path(canonical_target)
    if not executable.exists() or not manifest.exists():
        return {"ok": True, "installed": False, "current": False, "target": str(canonical_target), "version": None, "executable": str(executable)}
    require_regular_file(executable, "Qoder CLI executable")
    info = load_json_object(manifest, "software manifest", owner_only=True)
    current = info.get("schema_version") == 1 and info.get("version") == TESTED_VERSION and info.get("executable") == f"bin/{COMMAND_NAME}"
    return {"ok": True, "installed": True, "current": current, "target": str(canonical_target), "version": info.get("version"), "executable": str(executable)}


def download_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": PRODUCT_NAME})
    with urllib.request.urlopen(request, timeout=PROCESS_TIMEOUT_SECONDS) as response:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 256 * 1024 * 1024:
                fail("Qoder CLI download exceeded the 256 MiB bound")
            chunks.append(chunk)
    return b"".join(chunks)


def safe_tar_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    for member in members:
        path = Path(member.name)
        if path.is_absolute() or ".." in path.parts:
            fail(f"unsafe archive member path: {member.name}")
        if member.issym() or member.islnk() or member.isdev():
            fail(f"unsafe archive member type: {member.name}")
    return members


def install_or_update_cli(target: Path, *, operation: str) -> dict[str, Any]:
    canonical_target = ensure_target_directory(target)
    with target_lock(canonical_target):
        asset_key, asset = detect_platform_asset()
        url = asset.get("url")
        expected_sha = asset.get("sha256")
        if not isinstance(url, str) or not isinstance(expected_sha, str):
            fail(f"baseline asset {asset_key} is incomplete")
        archive_bytes = download_bytes(url)
        actual_sha = sha256_bytes(archive_bytes)
        if actual_sha != expected_sha:
            fail(f"downloaded Qoder CLI digest mismatch for {asset_key}")
        staging = canonical_target.parent / f".{canonical_target.name}.nddev-qoder-cli-software-stage"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(mode=OWNER_DIRECTORY_MODE)
        try:
            archive_path = staging / Path(url).name
            archive_path.write_bytes(archive_bytes)
            unpacked = staging / "unpacked"
            if archive_path.name.endswith(".zip"):
                with zipfile.ZipFile(archive_path) as archive:
                    for name in archive.namelist():
                        path = Path(name)
                        if path.is_absolute() or ".." in path.parts:
                            fail(f"unsafe archive member path: {name}")
                    archive.extractall(unpacked)
            else:
                with tarfile.open(archive_path, "r:gz") as archive:
                    archive.extractall(unpacked, members=safe_tar_members(archive))
            binary_source: Path | None = None
            for candidate in unpacked.rglob("*"):
                if candidate.name in {COMMAND_NAME, f"{COMMAND_NAME}.exe"} and candidate.is_file():
                    binary_source = candidate
                    break
            if binary_source is None:
                fail(f"archive {archive_path.name} did not contain a Qoder CLI executable")
            bin_dir = canonical_target / "bin"
            bin_dir.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
            executable = qoder_executable(canonical_target)
            temporary = bin_dir / f".{executable.name}.tmp"
            shutil.copy2(binary_source, temporary)
            temporary.chmod(0o700)
            os.replace(temporary, executable)
            manifest = {"schema_version": 1, "version": TESTED_VERSION, "executable": f"bin/{COMMAND_NAME}", "asset": asset_key, "sha256": expected_sha, "source": url}
            software_manifest_path(canonical_target).parent.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
            software_manifest_path(canonical_target).write_bytes(canonical_json(manifest))
            software_manifest_path(canonical_target).chmod(OWNER_FILE_MODE)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return {"ok": True, "operation": operation, "target": str(canonical_target), "version": TESTED_VERSION, "asset": asset_key, "executable": str(qoder_executable(canonical_target))}


def isolated_child_environment(target: Path) -> dict[str, str]:
    home = target / "home"
    runtime = target / "runtime"
    tmp = runtime / "tmp"
    for directory in (home, runtime, tmp):
        directory.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
        directory.chmod(OWNER_DIRECTORY_MODE)
    env: dict[str, str] = {}
    for name, value in os.environ.items():
        if name in TOKEN_ENV_NAMES:
            continue
        env[name] = value
    env.update({"HOME": str(home), "USERPROFILE": str(home), "QODER_HOME": str(target), "QODER_AUTO_UPDATE": "false", "TMPDIR": str(tmp), "XDG_CONFIG_HOME": str(runtime / "xdg-config"), "XDG_CACHE_HOME": str(runtime / "xdg-cache"), "XDG_STATE_HOME": str(runtime / "xdg-state"), "PATH": os.environ.get("PATH", "/usr/bin:/bin")})
    return env


def launch_qoder(target: Path, args: list[str]) -> int:
    canonical_target = require_explicit_absolute_target(str(target))
    state = inspect_target(canonical_target)
    if state["state"] != "managed":
        fail("target is not managed by nddev-qoder-cli-app")
    status = software_status(canonical_target)
    if not status["installed"] or not status["current"]:
        fail("Qoder CLI is not installed at the tested version in this target")
    child_args = list(state["launch_args"]) + ["--settings", str(canonical_target / "settings.json")] + args
    completed = subprocess.run([str(qoder_executable(canonical_target)), *child_args], cwd=os.getcwd(), env=isolated_child_environment(canonical_target), check=False, timeout=None)
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
