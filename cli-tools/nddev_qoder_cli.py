#!/usr/bin/env python3
"""Transactional setup manager for an explicit Qoder CLI target."""

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
import uuid
import zipfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows is rejected before locking.
    fcntl = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[1]
CATALOG_ROOT = ROOT / "setups"
PROFILE_ROOT = ROOT / "profiles"
BUILDER_ROOT = ROOT / "plugins" / "nddev-builder"
VERSION = (ROOT / "VERSION").read_text(encoding="ascii").strip()

PRODUCT_NAME = "nddev-qoder-cli-app"
COMMAND_NAME = "qodercli"
STAMP_NAME = "NDDEV-QODER-CLI-SETUP.json"
BACKUP_POOL_NAME = ".nddev-qoder-cli-backups"
BACKUP_NAME = "NDDEV-QODER-CLI-BACKUP.json"
CLEANUP_DIR_NAME = ".nddev-qoder-cli-cleanup"
CLEANUP_JOURNAL_NAME = "NDDEV-QODER-CLI-CLEANUP.json"
CLEANUP_INTENT_NAME = "NDDEV-QODER-CLI-CLEANUP-INTENT.json"
CLEANUP_PREPARE_INTENT_NAME = "NDDEV-QODER-CLI-PREPARE-INTENT.json"
CLEANUP_RETIRED_NAME = "NDDEV-QODER-CLI-CLEANUP-RETIRED.json"
SOFTWARE_MANIFEST_NAME = "qoder-cli.json"
BASELINE_REF = ROOT / "references" / "qoder-cli-baseline.json"

TESTED_VERSION = "1.1.10"
NPM_PACKAGE = "@qoder-ai/qodercli"
INSTALL_SCRIPT_URL = "https://qoder.com/install"
INSTALL_SCRIPT_SHA256 = "1e850fa3375bdccd179215f91638f4065d912c5dbddbd346d0ba352bad8ca9be"
INSTALL_SCRIPT_SIZE = 10651
MANIFEST_URL = "https://qoder-ide.oss-accelerate.aliyuncs.com/qodercli/channels/manifest.json"
MANIFEST_SHA256 = "f5aaf3ddcb5bba307173db5a51a72298c20f9cf2d32ba4e8f10b6e80b9cb6594"
MANIFEST_SIZE = 2717

DEFAULT_SETUP_ID = "nddev-builder"
DEFAULT_PROFILE_ID = "full-auto"
OWNER_FILE_MODE = 0o600
OWNER_EXECUTABLE_MODE = 0o700
OWNER_DIRECTORY_MODE = 0o700
METADATA_MAX_BYTES = 256 * 1024
MANAGED_PAYLOAD_MAX_BYTES = 8 * 1024 * 1024
PROCESS_TIMEOUT_SECONDS = 120
PROCESS_OUTPUT_MAX_BYTES = 256 * 1024
SOFTWARE_DOWNLOAD_MAX_BYTES = 50_589_197
SOFTWARE_TREE_MAX_BYTES = 512 * 1024 * 1024
SOFTWARE_TREE_MAX_PATHS = 5000
APPLEDOUBLE_METADATA_MAX_BYTES = 16 * 1024
MAX_BACKUP_SLOTS = 10
MAX_CLEANUP_ENTRIES = 24
MAX_CLEANUP_TREE_PATHS = 8000
MAX_CLEANUP_TREE_BYTES = 768 * 1024 * 1024
MAX_CLEANUP_JOURNAL_BYTES = METADATA_MAX_BYTES
MAX_PUBLICATION_ALIAS_SCAN = 4096

SETUP_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
PROFILE_ID_PATTERN = SETUP_ID_PATTERN
ANCHOR_TEMP_PATTERN = re.compile(r"\.[A-Za-z0-9._-]+\.pubtmp\.[0-9]+-[a-f0-9]+\Z")
JOURNAL_TEMP_PATTERN = re.compile(r"\.NDDEV-QODER-CLI-CLEANUP\.json\.pubtmp\.[0-9]+-[a-f0-9]+\Z")
CLEANUP_INTENT_TEMP_PATTERN = re.compile(
    r"\.NDDEV-QODER-CLI-CLEANUP-INTENT\.json\.pubtmp\.[0-9]+-[a-f0-9]+\Z"
)
PREPARE_TEMP_PATTERN = re.compile(
    r"\.NDDEV-QODER-CLI-PREPARE-INTENT\.json\.pubtmp\.[0-9]+-[a-f0-9]+\Z"
)
RETIRED_TEMP_PATTERN = re.compile(
    r"\.NDDEV-QODER-CLI-CLEANUP-RETIRED\.json\.pubtmp\.[0-9]+-[a-f0-9]+\Z"
)
WORK_ROOT_PATTERN = re.compile(r"\.work-[a-f0-9]{32}-[a-z0-9-]+\Z")
STAGE_ROOT_PATTERN = re.compile(r"\.stage-[a-f0-9]{32}\Z")

SUPPORTED_PRODUCT_HOST_IDS = (
    "macos-arm64",
    "macos-x64",
    "ubuntu-glibc-arm64",
    "ubuntu-glibc-x64",
)
UNSUPPORTED_CATEGORIES = (
    "windows",
    "non-ubuntu-linux",
    "linux-musl",
    "unsupported-architecture",
)
SOFTWARE_REPLACE_PATHS = (
    Path("bin") / COMMAND_NAME,
    Path("software") / SOFTWARE_MANIFEST_NAME,
)
SOFTWARE_PARENT_PATHS = tuple(
    sorted({relative.parent for relative in SOFTWARE_REPLACE_PATHS}, key=str)
)
MANAGED_FILE_PATHS = (
    Path("settings.json"),
    Path("AGENTS.md"),
    Path(STAMP_NAME),
)
MANAGED_PLUGIN_ROOT = Path("plugins") / "nddev-builder"
BUILDER_SOURCE_FILES = (
    (
        Path(".qoder-plugin") / "plugin.json",
        MANAGED_PLUGIN_ROOT / ".qoder-plugin" / "plugin.json",
    ),
    (
        Path("skills") / "nddev-builder" / "SKILL.md",
        MANAGED_PLUGIN_ROOT / "skills" / "nddev-builder" / "SKILL.md",
    ),
    (
        Path("agents") / "nddev-builder.md",
        MANAGED_PLUGIN_ROOT / "agents" / "nddev-builder.md",
    ),
    (
        Path("hooks") / "hooks.json",
        MANAGED_PLUGIN_ROOT / "hooks" / "hooks.json",
    ),
)
MANAGED_PATHS = (
    Path("settings.json"),
    Path("AGENTS.md"),
    *(target for _, target in BUILDER_SOURCE_FILES),
    Path(STAMP_NAME),
)
MANAGED_OBJECTS = (
    Path("settings.json"),
    Path("AGENTS.md"),
    MANAGED_PLUGIN_ROOT,
    Path(STAMP_NAME),
)
MANAGED_SETTINGS_KEYS = (
    "general",
    "permissions",
    "sandbox",
    "enabledPlugins",
    "extraKnownMarketplaces",
    "hooks",
    "tools",
)
STAMP_KEYS = {
    "schema_version",
    "product_name",
    "build_version",
    "setup_id",
    "profile_id",
    "permission_mode",
    "canonical_target",
    "trusted_workspace",
    "managed_files",
    "builder_projection",
    "launch_args",
    "cleanup_pending",
}
BACKUP_KEYS = {
    "schema_version",
    "product_name",
    "build_version",
    "slot",
    "canonical_target",
    "source_setup_id",
    "source_profile_id",
    "trusted_workspace",
    "managed_files",
    "file_records",
    "created_at",
}
PROFILE_KEYS = {
    "schema_version",
    "id",
    "description",
    "permission_mode",
    "launch_args",
    "default",
}
SETUP_KEYS = {
    "schema_version",
    "id",
    "description",
    "managed_files",
    "builder_projection",
    "builder_default_on",
}
ALLOWED_CREDENTIAL_ENV = ("QODER_PERSONAL_ACCESS_TOKEN",)
INHERITED_ENV = ("LANG", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM", "NO_COLOR", "CI")
FORBIDDEN_LAUNCH_ARGS = {
    "--add-dir",
    "--agents",
    "--allowed-mcp-server-names",
    "--allowed-tools",
    "--config-dir",
    "--continue",
    "--cwd",
    "--dangerously-skip-permissions",
    "--disallowed-tools",
    "--permission-mode",
    "--plugin-dir",
    "--resume",
    "--settings",
    "--tools",
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


class JsonArgumentError(Exception):
    """An argparse boundary error that can be serialized as JSON."""


class CleanupFailure(QoderCliSetupError):
    """A post-commit cleanup failure; the active state already committed."""


class CleanupJournalPublishedInvalid(QoderCliSetupError):
    """A final cleanup journal is visible but cannot prove valid pending state."""


class FinalPathPublishedFault(QoderCliSetupError):
    """A no-replace publication fault after the final pathname is visible."""

    def __init__(self, message: str, *, final_path: Path) -> None:
        super().__init__(message)
        self.final_path = final_path


def fail(message: str) -> None:
    raise QoderCliSetupError(message)


def fail_concurrent(message: str) -> None:
    raise ConcurrentTargetChange(message)


def fault(point: str) -> None:
    """Private validation seam. Public callers cannot trigger this by API or env."""
    del point


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def identity_of(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def owner_of(info: os.stat_result) -> Optional[int]:
    return info.st_uid if hasattr(info, "st_uid") else None


def current_uid() -> Optional[int]:
    return os.geteuid() if hasattr(os, "geteuid") else None


def mode_of(info: os.stat_result) -> int:
    return stat.S_IMODE(info.st_mode)


def is_owner_only_file(info: os.stat_result) -> bool:
    if not stat.S_ISREG(info.st_mode) or mode_of(info) != OWNER_FILE_MODE:
        return False
    uid = current_uid()
    return uid is None or owner_of(info) == uid


def is_owner_private_directory(info: os.stat_result) -> bool:
    if not stat.S_ISDIR(info.st_mode) or mode_of(info) != OWNER_DIRECTORY_MODE:
        return False
    uid = current_uid()
    return uid is None or owner_of(info) == uid


def is_owner_private_executable(info: os.stat_result) -> bool:
    if not stat.S_ISREG(info.st_mode) or mode_of(info) != OWNER_EXECUTABLE_MODE:
        return False
    uid = current_uid()
    return uid is None or owner_of(info) == uid


def path_exists_no_follow(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
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


def require_private_directory(path: Path, label: str) -> os.stat_result:
    info = require_directory(path, label)
    if not is_owner_private_directory(info):
        fail(f"{label} must be owned by the current user with mode 0700")
    return info


def require_regular_file(
    path: Path,
    label: str,
    *,
    owner_only: bool = False,
    max_bytes: int = MANAGED_PAYLOAD_MAX_BYTES,
    allow_nlink: Optional[int] = 1,
) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        fail(f"{label} is missing")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail(f"{label} must be a regular non-symlink file")
    if allow_nlink is not None and info.st_nlink != allow_nlink:
        fail(f"{label} must have link count {allow_nlink}")
    if owner_only and not is_owner_only_file(info):
        fail(f"{label} must be owned by the current user with mode 0600")
    if info.st_size > max_bytes:
        fail(f"{label} exceeds the {max_bytes}-byte size limit")
    return info


def require_safe_executable(path: Path, label: str) -> os.stat_result:
    info = require_regular_file(path, label, max_bytes=SOFTWARE_TREE_MAX_BYTES)
    if not is_owner_private_executable(info):
        fail(f"{label} must be owned by the current user with mode 0700")
    return info


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_parent(path: Path) -> None:
    fsync_directory(path.parent)


def write_all(fd: int, content: bytes, label: str) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(fd, content[offset:])
        if written <= 0:
            fail(f"{label} write made no progress")
        offset += written


def directory_snapshot(path: Path, label: str) -> dict[str, Any]:
    info = require_directory(path, label)
    return {
        "dev": info.st_dev,
        "ino": info.st_ino,
        "uid": owner_of(info),
        "mode": mode_of(info),
        "nlink": info.st_nlink,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "children": sorted(child.name for child in path.iterdir()),
    }


def restore_directory_snapshot(path: Path, snapshot: dict[str, Any], label: str) -> None:
    info = require_directory(path, label)
    if identity_of(info) != (snapshot["dev"], snapshot["ino"]):
        fail(f"{label} identity changed during rollback")
    if owner_of(info) != snapshot["uid"]:
        fail(f"{label} owner changed during rollback")
    children = sorted(child.name for child in path.iterdir())
    if children != snapshot["children"]:
        fail(f"{label} child set changed during rollback")
    os.chmod(path, int(snapshot["mode"]))
    os.utime(path, ns=(int(snapshot["mtime_ns"]), int(snapshot["mtime_ns"])))
    fsync_directory(path)


class PrivateDirectoryCreation:
    """Rollback only directories created by this operation before publication."""

    def __init__(self, *, stable_root: Optional[Path] = None) -> None:
        self.stable_root = stable_root
        self.created: list[Path] = []
        self.parent_snapshots: dict[Path, dict[str, Any]] = {}

    def _snapshot_parent(self, path: Path) -> None:
        parent = path.parent
        if parent not in self.parent_snapshots and path_exists_no_follow(parent):
            self.parent_snapshots[parent] = directory_snapshot(parent, f"{path} parent")

    def ensure(self, path: Path, label: str) -> None:
        if path_exists_no_follow(path):
            require_private_directory(path, label)
            return
        missing: list[Path] = []
        current = path
        while not path_exists_no_follow(current):
            missing.append(current)
            if current.parent == current:
                fail(f"{label} path escaped the filesystem root")
            current = current.parent
        if self.stable_root is not None:
            try:
                current.relative_to(self.stable_root)
            except ValueError:
                if current != self.stable_root:
                    fail(f"{label} path escaped the stable root")
        if current == self.stable_root:
            require_directory(current, "stable lifecycle lock root")
        else:
            require_private_directory(current, f"{label} existing ancestor")
        for directory in reversed(missing):
            self._snapshot_parent(directory)
            directory.mkdir(mode=OWNER_DIRECTORY_MODE)
            os.chmod(directory, OWNER_DIRECTORY_MODE)
            fsync_parent(directory)
            self.created.append(directory)

    def rollback(self) -> None:
        for directory in reversed(self.created):
            if not path_exists_no_follow(directory):
                continue
            info = directory.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                fail(f"created private directory changed before rollback: {directory}")
            try:
                directory.rmdir()
            except OSError as exc:
                fail(f"created private directory is not empty during rollback: {directory}: {exc}")
            fsync_parent(directory)
        created_set = set(self.created)
        for parent, snapshot in self.parent_snapshots.items():
            if parent in created_set and not path_exists_no_follow(parent):
                continue
            restore_directory_snapshot(parent, snapshot, f"{parent} rollback parent")


def read_regular_file(
    path: Path,
    label: str,
    *,
    owner_only: bool = False,
    max_bytes: int = MANAGED_PAYLOAD_MAX_BYTES,
    allow_nlink: Optional[int] = 1,
) -> tuple[bytes, os.stat_result]:
    before = require_regular_file(
        path,
        label,
        owner_only=owner_only,
        max_bytes=max_bytes,
        allow_nlink=allow_nlink,
    )
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    chunks: list[bytes] = []
    total = 0
    try:
        opened = os.fstat(descriptor)
        if identity_of(opened) != identity_of(before):
            fail_concurrent(f"{label} changed while it was opened")
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
    final = require_regular_file(
        path,
        label,
        owner_only=owner_only,
        max_bytes=max_bytes,
        allow_nlink=allow_nlink,
    )
    expected = identity_of(before)
    if identity_of(after) != expected or identity_of(final) != expected:
        fail_concurrent(f"{label} changed while it was read")
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
            f"{label} has invalid keys (missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)})"
        )


def validate_id(value: str, pattern: re.Pattern[str], label: str) -> None:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        fail(f"invalid {label}: {value!r}")


def validate_setup_id(setup_id: str) -> None:
    validate_id(setup_id, SETUP_ID_PATTERN, "setup id")


def validate_profile_id(profile_id: str) -> None:
    validate_id(profile_id, PROFILE_ID_PATTERN, "profile id")


def normalize_relative_path(raw: str, label: str) -> Path:
    if not isinstance(raw, str):
        fail(f"{label} path must be a string")
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        fail(f"{label} contains an unsafe relative path")
    return relative


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


def ensure_private_directory(path: Path) -> None:
    if path_exists_no_follow(path):
        require_private_directory(path, str(path))
        return
    missing: list[Path] = []
    current = path
    while not path_exists_no_follow(current):
        missing.append(current)
        current = current.parent
    require_private_directory(current, f"{path} existing ancestor")
    for directory in reversed(missing):
        directory.mkdir(mode=OWNER_DIRECTORY_MODE)
        os.chmod(directory, OWNER_DIRECTORY_MODE)
        fsync_parent(directory)


def ensure_private_parent(path: Path, label: str) -> None:
    parents: list[Path] = []
    current = path.parent
    while not path_exists_no_follow(current):
        parents.append(current)
        current = current.parent
    require_private_directory(current, f"{label} existing ancestor")
    for directory in reversed(parents):
        directory.mkdir(mode=OWNER_DIRECTORY_MODE)
        os.chmod(directory, OWNER_DIRECTORY_MODE)
        fsync_parent(directory)


def write_durable_file(path: Path, content: bytes, *, mode: int = OWNER_FILE_MODE) -> None:
    ensure_private_parent(path, f"file {path}")
    temporary: Optional[Path] = None
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd, tmp_raw = tempfile.mkstemp(prefix=f".{path.name}.tmp.", dir=str(path.parent))
        temporary = Path(tmp_raw)
        try:
            write_all(fd, content, f"file {path}")
            fault("write-file-write")
            os.fchmod(fd, mode)
            fault("write-file-fchmod")
            os.fsync(fd)
            fault("write-file-fsync")
        finally:
            os.close(fd)
        os.replace(temporary, path)
        temporary = None
        fault("write-file-replace")
        fsync_parent(path)
        fault("write-file-parent-fsync")
    except BaseException:
        if temporary is not None:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
            with contextlib.suppress(Exception):
                fsync_parent(temporary)
        raise


def publish_no_replace_file(
    final_path: Path,
    content: bytes,
    *,
    temp_pattern: re.Pattern[str],
    label: str,
    stable_parent_root: Optional[Path] = None,
) -> bool:
    """Publish complete content with a hard-link no-replace final-path commit.

    Returns True when this call created the final path. Returns False when a
    valid existing final path won the no-replace race. After the hard link makes
    final_path visible, the final file is monotonic and is never unlinked here.
    """

    if len(content) > METADATA_MAX_BYTES:
        fail(f"{label} exceeds the {METADATA_MAX_BYTES}-byte metadata size limit")
    parent_creation = PrivateDirectoryCreation(stable_root=stable_parent_root)
    parent_creation.ensure(final_path.parent, f"{label} parent")
    token = f"{os.getpid()}-{uuid.uuid4().hex}"
    temporary = final_path.parent / f".{final_path.name}.pubtmp.{token}"
    if not temp_pattern.fullmatch(temporary.name):
        fail(f"{label} temporary publication name is outside the declared grammar")
    fd = -1
    linked = False
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            OWNER_FILE_MODE,
        )
        fault(f"{label}:binding-temp-create")
        write_all(fd, content, label)
        fault(f"{label}:binding-write")
        os.fchmod(fd, OWNER_FILE_MODE)
        fault(f"{label}:binding-fchmod")
        os.fsync(fd)
        fault(f"{label}:binding-file-fsync")
        os.close(fd)
        fd = -1
        fault(f"{label}:binding-close")
        try:
            os.link(temporary, final_path)
            linked = True
            fault(f"{label}:binding-no-replace")
        except FileExistsError:
            try:
                temporary.unlink()
            finally:
                try:
                    fsync_parent(final_path)
                finally:
                    parent_creation.rollback()
            return False
        try:
            fsync_parent(final_path)
            fault(f"{label}:binding-parent-fsync")
            fault(f"{label}:binding-alias-unlink")
            temporary.unlink()
            fsync_parent(final_path)
            fault(f"{label}:binding-alias-parent-fsync")
        except BaseException:
            # Final-path visibility is the commit point. Keep final_path and
            # leave only the unpublished alias for mutation-only recovery.
            raise FinalPathPublishedFault(
                f"{label} publication reached final path but cleanup/durability failed",
                final_path=final_path,
            )
        return True
    except BaseException:
        if fd >= 0:
            os.close(fd)
        if not linked:
            try:
                with contextlib.suppress(FileNotFoundError):
                    temporary.unlink()
            finally:
                try:
                    fsync_parent(final_path)
                finally:
                    parent_creation.rollback()
        raise


def recover_hardlink_publication_alias(
    final_path: Path,
    *,
    temp_pattern: re.Pattern[str],
    label: str,
) -> None:
    final_info = require_regular_file(
        final_path,
        label,
        owner_only=True,
        max_bytes=METADATA_MAX_BYTES,
        allow_nlink=None,
    )
    if final_info.st_nlink == 1:
        return
    if final_info.st_nlink != 2:
        fail(f"{label} has unsupported hard-link count {final_info.st_nlink}")
    matches: list[Path] = []
    scanned = 0
    for entry in final_path.parent.iterdir():
        scanned += 1
        if scanned > MAX_PUBLICATION_ALIAS_SCAN:
            fail(f"{label} parent has too many entries for bounded alias recovery")
        if entry == final_path or not temp_pattern.fullmatch(entry.name):
            continue
        try:
            entry_info = entry.lstat()
        except FileNotFoundError:
            continue
        if identity_of(entry_info) == identity_of(final_info):
            matches.append(entry)
    if len(matches) != 1:
        fail(f"{label} has no exact recoverable publication alias")
    matches[0].unlink()
    fsync_parent(final_path)
    recovered = require_regular_file(
        final_path,
        label,
        owner_only=True,
        max_bytes=METADATA_MAX_BYTES,
        allow_nlink=1,
    )
    if identity_of(recovered) != identity_of(final_info):
        fail(f"{label} changed during publication alias recovery")


def load_strict_json_file(path: Path, label: str, *, read_only: bool) -> dict[str, Any]:
    info = require_regular_file(
        path,
        label,
        owner_only=True,
        max_bytes=METADATA_MAX_BYTES,
        allow_nlink=None,
    )
    if info.st_nlink != 1:
        if read_only:
            fail(f"{label} publication is incomplete")
        fail(f"{label} publication must be recovered under mutation coordination")
    content, _ = read_regular_file(path, label, owner_only=True, max_bytes=METADATA_MAX_BYTES)
    return parse_json_object(content, label)


def read_regular_fd(
    fd: int, label: str, *, max_bytes: int = METADATA_MAX_BYTES
) -> tuple[bytes, os.stat_result]:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        fail(f"{label} must be a regular file")
    if info.st_size > max_bytes:
        fail(f"{label} exceeds the {max_bytes}-byte size limit")
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            fail(f"{label} exceeds the {max_bytes}-byte size limit")
        chunks.append(chunk)
    return b"".join(chunks), os.fstat(fd)


def fixed_temp_root() -> Path:
    """Return the platform rendezvous root, independent of caller TMPDIR."""

    candidates = (
        [Path("/private/tmp"), Path("/tmp")] if sys.platform == "darwin" else [Path("/tmp")]
    )
    root: Optional[Path] = None
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        try:
            info = resolved.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            continue
        mode = mode_of(info)
        if mode & stat.S_ISVTX and os.access(resolved, os.W_OK | os.X_OK):
            root = resolved
            break
    if root is None:
        fail("no supported stable lifecycle lock root is available")
    return root


def lock_pool_path() -> Path:
    uid = current_uid()
    suffix = str(uid) if uid is not None else "unknown"
    return fixed_temp_root() / f".{PRODUCT_NAME}-{suffix}-lifecycle-locks"


def ensure_lock_directory(path: Path) -> None:
    root = fixed_temp_root()
    if path_exists_no_follow(path):
        require_private_directory(path, str(path))
        return
    creation = PrivateDirectoryCreation(stable_root=root)
    creation.ensure(path, f"lifecycle lock directory {path}")


def product_anchor_path() -> Path:
    return lock_pool_path() / "global.lock"


def target_digest(canonical_target: Path) -> str:
    return sha256_bytes(str(canonical_target).encode("utf-8"))


def target_anchor_path(canonical_target: Path) -> Path:
    return lock_pool_path() / "targets" / f"{target_digest(canonical_target)}.lock"


def anchor_binding(
    kind: str, path: Path, canonical_target: Optional[Path] = None
) -> dict[str, Any]:
    binding: dict[str, Any] = {
        "schema_version": 1,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "kind": kind,
        "uid": current_uid(),
    }
    if canonical_target is not None:
        binding["canonical_target"] = str(canonical_target)
        binding["target_digest"] = target_digest(canonical_target)
    else:
        binding["canonical_target"] = None
        binding["target_digest"] = None
    binding["binding_sha256"] = sha256_bytes(
        canonical_json(
            {
                "product_name": PRODUCT_NAME,
                "kind": kind,
                "canonical_target": binding["canonical_target"],
                "target_digest": binding["target_digest"],
                "path": str(path),
            }
        )
    )
    return binding


def expected_anchor_value(
    path: Path, kind: str, canonical_target: Optional[Path]
) -> dict[str, Any]:
    return anchor_binding(kind, path, canonical_target)


def validate_anchor_fd(
    fd: int,
    path: Path,
    kind: str,
    canonical_target: Optional[Path],
    *,
    allow_alias: bool,
) -> os.stat_result:
    label = f"{kind} lifecycle anchor"
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or not is_owner_only_file(info):
        fail(f"{label} must be an owner-only regular file")
    if info.st_nlink != 1 and not (allow_alias and info.st_nlink == 2):
        fail(f"{label} publication is incomplete")
    final_info = require_regular_file(
        path,
        label,
        owner_only=True,
        max_bytes=METADATA_MAX_BYTES,
        allow_nlink=None,
    )
    if identity_of(final_info) != identity_of(info):
        fail_concurrent(f"{label} changed while it was opened")
    content, after = read_regular_fd(fd, label, max_bytes=METADATA_MAX_BYTES)
    if identity_of(after) != identity_of(info):
        fail_concurrent(f"{label} changed while it was read")
    value = parse_json_object(content, label)
    expected = expected_anchor_value(path, kind, canonical_target)
    if value != expected:
        fail(f"{label} binding is malformed or mismatched")
    return after


def validate_anchor_binding(
    path: Path, kind: str, canonical_target: Optional[Path], *, read_only: bool
) -> None:
    label = f"{kind} lifecycle anchor"
    value = load_strict_json_file(path, label, read_only=read_only)
    expected = expected_anchor_value(path, kind, canonical_target)
    if value != expected:
        fail(f"{label} binding is malformed or mismatched")


def open_anchor_fd(
    path: Path, kind: str, canonical_target: Optional[Path], *, read_only: bool
) -> int:
    flags = os.O_RDWR if not read_only else os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        validate_anchor_fd(fd, path, kind, canonical_target, allow_alias=not read_only)
        flock_fd(fd, shared=read_only)
        fault(f"{kind}-anchor-lock-acquired")
        validate_anchor_fd(fd, path, kind, canonical_target, allow_alias=not read_only)
        if not read_only:
            recover_hardlink_publication_alias(
                path,
                temp_pattern=ANCHOR_TEMP_PATTERN,
                label=f"{kind} lifecycle anchor",
            )
            validate_anchor_fd(fd, path, kind, canonical_target, allow_alias=False)
    except BaseException:
        os.close(fd)
        raise
    return fd


def flock_fd(fd: int, *, shared: bool) -> None:
    if fcntl is None:
        fail("fcntl locking is unavailable on this host")
    operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    fcntl.flock(fd, operation)


@contextlib.contextmanager
def product_lifecycle_lock(*, shared: bool, create: bool) -> Iterator[Optional[int]]:
    path = product_anchor_path()
    if not create and not path_exists_no_follow(path):
        yield None
        return
    if create:
        publish_no_replace_file(
            path,
            canonical_json(anchor_binding("product", path, None)),
            temp_pattern=ANCHOR_TEMP_PATTERN,
            label="product-anchor",
            stable_parent_root=fixed_temp_root(),
        )
    fd = open_anchor_fd(path, "product", None, read_only=shared)
    try:
        yield fd
    finally:
        if fcntl is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def target_lifecycle_lock(
    canonical_target: Path,
    *,
    shared: bool,
    create: bool,
) -> Iterator[Optional[int]]:
    path = target_anchor_path(canonical_target)
    if not create and not path_exists_no_follow(path):
        yield None
        return
    if create:
        publish_no_replace_file(
            path,
            canonical_json(anchor_binding("target", path, canonical_target)),
            temp_pattern=ANCHOR_TEMP_PATTERN,
            label="target-anchor",
            stable_parent_root=fixed_temp_root(),
        )
    fd = open_anchor_fd(path, "target", canonical_target, read_only=shared)
    try:
        yield fd
    finally:
        if fcntl is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def require_supported_product_host(host: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    facts = host if host is not None else current_host_facts()
    system = str(facts.get("system", "")).lower()
    machine = str(facts.get("machine", "")).lower()
    arch: Optional[str]
    if machine in {"arm64", "aarch64"}:
        arch = "arm64"
    elif machine in {"x86_64", "amd64", "x64"}:
        arch = "x64"
    else:
        fail("unsupported host category: unsupported-architecture")

    if system == "darwin":
        host_id = f"macos-{arch}"
    elif system in {"windows", "win32", "cygwin"}:
        fail("unsupported host category: windows")
    elif system == "linux":
        libc = str(facts.get("libc", "")).lower()
        distro_id = str(facts.get("distro_id", "")).lower()
        if "musl" in libc:
            fail("unsupported host category: linux-musl")
        if distro_id != "ubuntu":
            fail("unsupported host category: non-ubuntu-linux")
        host_id = f"ubuntu-glibc-{arch}"
    else:
        fail("unsupported host category: unsupported-architecture")

    if host_id not in SUPPORTED_PRODUCT_HOST_IDS:
        fail("unsupported host category: unsupported-architecture")
    result = dict(facts)
    result["product_host_id"] = host_id
    return result


def current_host_facts() -> dict[str, Any]:
    distro_id = ""
    if sys.platform.startswith("linux"):
        try:
            for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
                if line.startswith("ID="):
                    distro_id = line.split("=", 1)[1].strip().strip('"').lower()
                    break
        except OSError:
            distro_id = ""
    libc_name, libc_version = platform.libc_ver()
    return {
        "system": platform.system().lower(),
        "machine": platform.machine().lower(),
        "distro_id": distro_id,
        "libc": libc_name.lower(),
        "libc_version": libc_version,
        "avx2": cpu_has_avx2(),
    }


def cpu_has_avx2() -> bool:
    if not sys.platform.startswith("linux") and sys.platform != "darwin":
        return False
    try:
        if sys.platform.startswith("linux"):
            text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="ignore")
            return " avx2 " in f" {text.lower()} "
        completed = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "hw.optional.avx2_0"],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
        return completed.stdout.strip() == "1"
    except Exception:
        return False


def select_platform_asset(host: Optional[dict[str, Any]] = None) -> tuple[str, dict[str, Any]]:
    facts = require_supported_product_host(host)
    host_id = facts["product_host_id"]
    if host_id == "macos-arm64":
        asset_key = "darwin-arm64"
    elif host_id == "macos-x64":
        asset_key = "darwin-amd64"
    elif host_id == "ubuntu-glibc-arm64":
        asset_key = "linux-arm64"
    elif host_id == "ubuntu-glibc-x64":
        asset_key = "linux-amd64" if facts.get("avx2") else "linux-amd64-baseline"
    else:
        fail("unsupported host category: unsupported-architecture")
    baseline = load_baseline()
    files = baseline.get("manifest", {}).get("files", {})
    asset = files.get(asset_key)
    if not isinstance(asset, dict):
        fail(f"baseline does not declare asset {asset_key}")
    return asset_key, asset


def require_lexical_absolute_target(raw_target: Optional[str]) -> Path:
    if not raw_target:
        fail("an explicit --target absolute path is required")
    target = Path(raw_target)
    if not target.is_absolute():
        fail("--target must be an absolute path")
    if any(part in {"", ".", ".."} for part in target.parts[1:]):
        fail("--target must be a lexical absolute path without '.' or '..'")
    return target


def resolve_target_under_coordination(target: Path, *, mutation: bool) -> Path:
    try:
        info = target.lstat()
    except FileNotFoundError:
        parent = target.parent
        if not path_exists_no_follow(parent):
            if mutation:
                fail("target parent is missing")
            return target
        try:
            resolved_parent = parent.resolve(strict=True)
        except FileNotFoundError:
            if mutation:
                fail("target parent is missing")
            return target
        parent_info = require_directory(resolved_parent, "target parent")
        if mutation and not is_owner_private_directory(parent_info):
            fail("target parent must be private to the current user with mode 0700")
        return resolved_parent / target.name
    if stat.S_ISLNK(info.st_mode):
        fail("--target must not be a symlink")
    if not stat.S_ISDIR(info.st_mode):
        fail("--target must be a directory")
    canonical = target.resolve(strict=True)
    target_info = require_directory(canonical, "target")
    if not is_owner_private_directory(target_info):
        fail("target must be private to the current user with mode 0700")
    return canonical


def coordinate_target(
    raw_target: str,
    *,
    mutation: bool,
    action: Callable[[Path], dict[str, Any]],
) -> dict[str, Any]:
    require_supported_product_host()
    lexical = require_lexical_absolute_target(raw_target)
    if mutation:
        with product_lifecycle_lock(shared=False, create=True):
            canonical = resolve_target_under_coordination(lexical, mutation=True)
            with target_lifecycle_lock(canonical, shared=False, create=True):
                drain_cleanup_before_mutation(canonical)
                return action(canonical)

    product_path = product_anchor_path()
    if not path_exists_no_follow(product_path):
        canonical = resolve_target_under_coordination(lexical, mutation=False)
        target_path = target_anchor_path(canonical)
        if path_exists_no_follow(target_path):
            fail("canonical target lifecycle anchor exists without product anchor")
        result = action(canonical)
        if path_exists_no_follow(product_path):
            with product_lifecycle_lock(shared=True, create=False):
                canonical = resolve_target_under_coordination(lexical, mutation=False)
                target_path = target_anchor_path(canonical)
                if path_exists_no_follow(target_path):
                    with target_lifecycle_lock(canonical, shared=True, create=False):
                        return action(canonical)
                return action(canonical)
        if path_exists_no_follow(target_path):
            fail_concurrent("canonical target lifecycle anchor appeared without product anchor")
        return result
    with product_lifecycle_lock(shared=True, create=False):
        canonical = resolve_target_under_coordination(lexical, mutation=False)
        target_path = target_anchor_path(canonical)
        if path_exists_no_follow(target_path):
            with target_lifecycle_lock(canonical, shared=True, create=False):
                return action(canonical)
        return action(canonical)


def ensure_target_directory(target: Path) -> bool:
    if path_exists_no_follow(target):
        require_private_directory(target, "target")
        return False
    parent = target.parent
    parent_snapshot = directory_snapshot(parent, "target parent")
    parent_info = require_private_directory(parent, "target parent")
    if target.name in parent_snapshot["children"]:
        fail_concurrent("target appeared during setup")
    del parent_info
    try:
        target.mkdir(mode=OWNER_DIRECTORY_MODE)
        fault("target-directory-create")
        os.chmod(target, OWNER_DIRECTORY_MODE)
        fault("target-directory-chmod")
        fsync_parent(target)
        fault("target-directory-parent-fsync")
    except BaseException:
        if path_exists_no_follow(target):
            info = target.lstat()
            if stat.S_ISLNK(info.st_mode):
                fail("created target changed into a symlink during rollback")
            if stat.S_ISDIR(info.st_mode):
                if any(target.iterdir()):
                    fail("created target is not empty during rollback")
                target.rmdir()
            else:
                fail("created target changed into an unsafe object during rollback")
            fsync_parent(target)
        restore_directory_snapshot(parent, parent_snapshot, "target parent")
        raise
    return True


def remove_created_target(target: Path, parent_snapshot: dict[str, Any]) -> None:
    if path_exists_no_follow(target):
        info = target.lstat()
        if stat.S_ISLNK(info.st_mode):
            fail("created target changed into a symlink during rollback")
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(target)
        else:
            target.unlink()
        fsync_parent(target)
    restore_directory_snapshot(target.parent, parent_snapshot, "target parent")


def load_baseline() -> dict[str, Any]:
    return load_json_object(BASELINE_REF, "Qoder CLI baseline")


def managed_settings_view(settings: dict[str, Any]) -> dict[str, Any]:
    return {key: settings[key] for key in MANAGED_SETTINGS_KEYS if key in settings}


def merge_settings(
    existing: Optional[dict[str, Any]],
    setup_settings: dict[str, Any],
    *,
    profile: dict[str, Any],
    trusted_workspace: Path,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if existing is not None:
        for key, value in existing.items():
            if key not in MANAGED_SETTINGS_KEYS:
                result[key] = value
    managed = managed_settings_view(setup_settings)
    general = dict(managed.get("general", {}))
    general["defaultPermissionMode"] = profile["permission_mode"]
    general["enableAutoUpdate"] = False
    managed["general"] = general
    permissions = dict(managed.get("permissions", {}))
    permissions["trustDirectories"] = [str(trusted_workspace)]
    managed["permissions"] = permissions
    result.update(managed)
    return result


def validate_settings(settings: dict[str, Any], label: str) -> None:
    general = settings.get("general")
    permissions = settings.get("permissions")
    sandbox = settings.get("sandbox")
    if not isinstance(general, dict) or general.get("defaultPermissionMode") not in {
        "bypass_permissions",
        "dont_ask",
    }:
        fail(f"{label} has invalid general.defaultPermissionMode")
    if general.get("enableAutoUpdate") is not False:
        fail(f"{label} must disable automatic update")
    if not isinstance(permissions, dict):
        fail(f"{label} has invalid permissions")
    trust = permissions.get("trustDirectories")
    if trust is not None and (
        not isinstance(trust, list) or not all(isinstance(item, str) for item in trust)
    ):
        fail(f"{label} permissions.trustDirectories must be a string array")
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


def load_setup_metadata(setup_id: str) -> dict[str, Any]:
    validate_setup_id(setup_id)
    setup_root = CATALOG_ROOT / setup_id
    if not setup_root.is_dir() or setup_root.is_symlink():
        fail(f"unknown setup: {setup_id}")
    metadata = load_json_object(setup_root / "setup.json", f"setup {setup_id} metadata")
    require_exact_keys(metadata, SETUP_KEYS, f"setup {setup_id} metadata")
    if metadata["schema_version"] != 1 or metadata["id"] != setup_id:
        fail(f"setup {setup_id} metadata identity mismatch")
    if metadata["managed_files"] != ["settings.json", "AGENTS.md"]:
        fail(f"setup {setup_id} managed file declaration is invalid")
    if (
        metadata["builder_projection"] != "native-plugin"
        or metadata["builder_default_on"] is not True
    ):
        fail(f"setup {setup_id} must enable native builder projection")
    return metadata


def load_profile(profile_id: str) -> dict[str, Any]:
    validate_profile_id(profile_id)
    profile_root = PROFILE_ROOT / profile_id
    if not profile_root.is_dir() or profile_root.is_symlink():
        fail(f"unknown profile: {profile_id}")
    metadata = load_json_object(profile_root / "profile.json", f"profile {profile_id} metadata")
    require_exact_keys(metadata, PROFILE_KEYS, f"profile {profile_id} metadata")
    if metadata["schema_version"] != 1 or metadata["id"] != profile_id:
        fail(f"profile {profile_id} metadata identity mismatch")
    if metadata["permission_mode"] not in {"bypass_permissions", "dont_ask"}:
        fail(f"profile {profile_id} permission mode is unsupported")
    if metadata["launch_args"] != ["--permission-mode", metadata["permission_mode"]]:
        fail(f"profile {profile_id} launch_args must use canonical --permission-mode")
    if not isinstance(metadata["default"], bool):
        fail(f"profile {profile_id} default marker must be boolean")
    return metadata


def list_setups() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in sorted(CATALOG_ROOT.iterdir()):
        if not path.is_dir() or path.is_symlink():
            continue
        metadata = load_setup_metadata(path.name)
        result.append(
            {
                "id": metadata["id"],
                "description": metadata["description"],
                "builder_default_on": metadata["builder_default_on"],
            }
        )
    return result


def list_profiles() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in sorted(PROFILE_ROOT.iterdir()):
        if not path.is_dir() or path.is_symlink():
            continue
        profile = load_profile(path.name)
        result.append(
            {
                "id": profile["id"],
                "description": profile["description"],
                "permission_mode": profile["permission_mode"],
                "launch_args": profile["launch_args"],
                "default": profile["default"],
            }
        )
    return result


def render_builder_files() -> dict[Path, bytes]:
    files: dict[Path, bytes] = {}
    for source_relative, target_relative in BUILDER_SOURCE_FILES:
        content, _ = read_regular_file(
            BUILDER_ROOT / source_relative,
            f"builder source {source_relative}",
            max_bytes=MANAGED_PAYLOAD_MAX_BYTES,
            allow_nlink=None,
        )
        files[target_relative] = content
    return files


def managed_digest(relative: Path, content: bytes) -> str:
    if relative == Path("settings.json"):
        settings = parse_json_object(content, str(relative))
        return sha256_bytes(canonical_json(managed_settings_view(settings)))
    return sha256_bytes(content)


def build_stamp(
    setup_id: str,
    profile: dict[str, Any],
    trusted_workspace: Path,
    desired: dict[Path, bytes],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "setup_id": setup_id,
        "profile_id": profile["id"],
        "permission_mode": profile["permission_mode"],
        "canonical_target": "",
        "trusted_workspace": str(trusted_workspace),
        "managed_files": {
            str(relative): managed_digest(relative, content)
            for relative, content in desired.items()
            if relative != Path(STAMP_NAME)
        },
        "builder_projection": "qoder-native-local-plugin",
        "launch_args": profile["launch_args"],
        "cleanup_pending": False,
    }


def bind_stamp(stamp: dict[str, Any], canonical_target: Path) -> dict[str, Any]:
    bound = dict(stamp)
    bound["canonical_target"] = str(canonical_target)
    return bound


def canonical_workspace(raw_workspace: Optional[str], *, default: Path) -> Path:
    workspace = Path(raw_workspace) if raw_workspace else default
    if not workspace.is_absolute():
        fail("--workspace must be an absolute path")
    info = require_directory(workspace, "trusted workspace")
    if stat.S_ISLNK(info.st_mode):
        fail("--workspace must not be a symlink")
    return workspace.resolve(strict=True)


def render_setup(
    setup_id: str,
    profile_id: str,
    *,
    canonical_target: Path,
    trusted_workspace: Path,
    existing_settings: Optional[dict[str, Any]] = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[Path, bytes]]:
    setup = load_setup_metadata(setup_id)
    profile = load_profile(profile_id)
    setup_root = CATALOG_ROOT / setup_id
    settings = load_json_object(setup_root / "settings.json", f"setup {setup_id}/settings.json")
    validate_settings(settings, f"setup {setup_id}/settings.json")
    agents_md, _ = read_regular_file(setup_root / "AGENTS.md", f"setup {setup_id}/AGENTS.md")
    desired: dict[Path, bytes] = {
        Path("settings.json"): canonical_json(
            merge_settings(
                existing_settings,
                settings,
                profile=profile,
                trusted_workspace=trusted_workspace,
            )
        ),
        Path("AGENTS.md"): agents_md,
    }
    desired.update(render_builder_files())
    stamp = bind_stamp(build_stamp(setup_id, profile, trusted_workspace, desired), canonical_target)
    desired[Path(STAMP_NAME)] = canonical_json(stamp)
    return setup, profile, desired


def validate_managed_destination(target: Path, relative: Path) -> None:
    if relative.is_absolute() or ".." in relative.parts:
        fail(f"unsafe managed path: {relative}")
    reject_relative_symlink_ancestors(target, relative)
    path = target / relative
    if not path_exists_no_follow(path):
        return
    if relative == MANAGED_PLUGIN_ROOT:
        require_private_directory(path, f"managed tree root {relative}")
        return
    require_regular_file(path, f"managed file {relative}", owner_only=True)


def validate_managed_plugin_tree(target: Path) -> None:
    root = target / MANAGED_PLUGIN_ROOT
    if not path_exists_no_follow(root):
        return
    require_private_directory(root, "managed plugin root")
    expected = {
        str(relative.relative_to(MANAGED_PLUGIN_ROOT)) for _, relative in BUILDER_SOURCE_FILES
    }
    expected_dirs = set()
    for raw in expected:
        path = Path(raw)
        for index in range(1, len(path.parts)):
            expected_dirs.add(Path(*path.parts[:index]).as_posix())
    actual_files: set[str] = set()
    actual_dirs: set[str] = set()
    for entry in root.rglob("*"):
        relative = entry.relative_to(root).as_posix()
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode):
            fail(f"managed plugin tree contains symlink: {relative}")
        if stat.S_ISDIR(info.st_mode):
            if not is_owner_private_directory(info):
                fail(f"managed plugin directory must be private: {relative}")
            actual_dirs.add(relative)
        elif stat.S_ISREG(info.st_mode):
            if not is_owner_only_file(info):
                fail(f"managed plugin file must be private: {relative}")
            actual_files.add(relative)
        else:
            fail(f"managed plugin tree contains unsupported object: {relative}")
    if actual_files - expected:
        fail(f"managed plugin tree contains unmanaged files: {sorted(actual_files - expected)}")
    if actual_dirs - expected_dirs:
        fail(
            f"managed plugin tree contains unmanaged directories: {sorted(actual_dirs - expected_dirs)}"
        )


def any_managed_path_exists(target: Path) -> bool:
    return any(path_exists_no_follow(target / relative) for relative in MANAGED_PATHS)


def load_stamp(target: Path) -> Optional[dict[str, Any]]:
    stamp = target / STAMP_NAME
    if not path_exists_no_follow(stamp):
        return None
    value = load_json_object(stamp, "setup stamp", owner_only=True)
    require_exact_keys(value, STAMP_KEYS, "setup stamp")
    if value["schema_version"] != 1 or value["product_name"] != PRODUCT_NAME:
        fail("setup stamp is not compatible with this product")
    if value["build_version"] != VERSION:
        fail("setup stamp is not compatible with this build")
    if value["canonical_target"] != str(target):
        fail("setup stamp is bound to a different canonical target")
    if not isinstance(value["managed_files"], dict):
        fail("setup stamp managed_files must be an object")
    validate_setup_id(value["setup_id"])
    validate_profile_id(value["profile_id"])
    if value["permission_mode"] not in {"bypass_permissions", "dont_ask"}:
        fail("setup stamp has unsupported permission mode")
    if not isinstance(value["launch_args"], list) or value["launch_args"] != [
        "--permission-mode",
        value["permission_mode"],
    ]:
        fail("setup stamp launch arguments are malformed")
    if (
        not isinstance(value["trusted_workspace"], str)
        or not Path(value["trusted_workspace"]).is_absolute()
    ):
        fail("setup stamp trusted workspace is malformed")
    if value["cleanup_pending"] is not False:
        fail("setup stamp cleanup_pending must be false; cleanup state is reported separately")
    return value


def validate_managed_files(target: Path, stamp: dict[str, Any]) -> list[str]:
    validate_managed_plugin_tree(target)
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


def read_existing_settings_if_managed(
    target: Path, state: dict[str, Any]
) -> Optional[dict[str, Any]]:
    if state.get("state") != "managed":
        return None
    return load_json_object(target / "settings.json", "existing settings.json", owner_only=True)


def cleanup_parent(target: Path) -> Path:
    return target / CLEANUP_DIR_NAME


def cleanup_journal_path(target: Path) -> Path:
    return cleanup_parent(target) / CLEANUP_JOURNAL_NAME


def cleanup_intent_path(target: Path) -> Path:
    return cleanup_parent(target) / CLEANUP_INTENT_NAME


def cleanup_prepare_intent_path(target: Path) -> Path:
    return cleanup_parent(target) / CLEANUP_PREPARE_INTENT_NAME


def cleanup_retired_path(target: Path) -> Path:
    return cleanup_parent(target) / CLEANUP_RETIRED_NAME


def relative_to_target(target: Path, path: Path) -> Path:
    try:
        return path.relative_to(target)
    except ValueError:
        fail(f"path is outside target: {path}")
    raise AssertionError("unreachable")


def file_record_for_path(
    root: Path, relative: Path, *, max_bytes: int = MANAGED_PAYLOAD_MAX_BYTES
) -> dict[str, Any]:
    path = root / relative
    content, info = read_regular_file(
        path, f"file record {relative}", owner_only=True, max_bytes=max_bytes
    )
    return {
        "path": relative.as_posix(),
        "size": info.st_size,
        "sha256": sha256_bytes(content),
    }


def stat_record(path: Path, root: Path) -> dict[str, Any]:
    info = path.lstat()
    relative = "." if path == root else path.relative_to(root).as_posix()
    kind = (
        "directory"
        if stat.S_ISDIR(info.st_mode)
        else "file"
        if stat.S_ISREG(info.st_mode)
        else "other"
    )
    record = {
        "relative_path": relative,
        "kind": kind,
        "mode": mode_of(info),
        "uid": owner_of(info),
        "nlink": info.st_nlink,
        "dev": info.st_dev,
        "ino": info.st_ino,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
    }
    if kind == "file":
        content, _ = read_regular_file(
            path, f"cleanup file {relative}", max_bytes=MAX_CLEANUP_TREE_BYTES
        )
        record["sha256"] = sha256_bytes(content)
    return record


def tree_records(root: Path) -> list[dict[str, Any]]:
    info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        fail("cleanup source must be a regular file or real directory")
    records = [stat_record(root, root)]
    if stat.S_ISREG(info.st_mode):
        return records
    total_paths = 1
    total_bytes = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        total_paths += 1
        if total_paths > MAX_CLEANUP_TREE_PATHS:
            fail("cleanup tree exceeds path bound")
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            fail(f"cleanup tree contains symlink: {path.relative_to(root)}")
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            fail(f"cleanup tree contains unsupported object: {path.relative_to(root)}")
        if stat.S_ISREG(info.st_mode):
            total_bytes += info.st_size
            if total_bytes > MAX_CLEANUP_TREE_BYTES:
                fail("cleanup tree exceeds byte bound")
        records.append(stat_record(path, root))
    return records


def cleanup_tree_sha256(records: list[dict[str, Any]]) -> str:
    return sha256_bytes(canonical_json(records))


def validate_bounded_relative(
    raw: str, label: str, *, first_pattern: Optional[re.Pattern[str]] = None
) -> Path:
    path = normalize_relative_path(raw, label)
    if len(path.parts) > 24 or any(len(part) > 128 for part in path.parts):
        fail(f"{label} path exceeds bound")
    if first_pattern is not None and not first_pattern.fullmatch(path.parts[0]):
        fail(f"{label} path is outside the declared manager namespace")
    return path


def anchored_path(target: Path, anchor: str, relative: str, label: str) -> Path:
    path = validate_bounded_relative(
        relative,
        label,
        first_pattern=WORK_ROOT_PATTERN if anchor == CLEANUP_DIR_NAME else None,
    )
    if anchor == "target":
        return target / path
    if anchor == CLEANUP_DIR_NAME:
        return cleanup_parent(target) / path
    fail(f"{label} anchor is unsupported")


def object_identity_record(path: Path, anchor_root: Path, label: str) -> dict[str, Any]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        fail(f"{label} is missing")
    if stat.S_ISLNK(info.st_mode):
        fail(f"{label} must not be a symlink")
    kind = (
        "directory"
        if stat.S_ISDIR(info.st_mode)
        else "file"
        if stat.S_ISREG(info.st_mode)
        else "other"
    )
    if kind == "other":
        fail(f"{label} has unsupported object kind")
    try:
        relative = "." if path == anchor_root else path.relative_to(anchor_root).as_posix()
    except ValueError:
        fail(f"{label} is outside its declared anchor")
    record: dict[str, Any] = {
        "relative_path": relative,
        "kind": kind,
        "mode": mode_of(info),
        "uid": owner_of(info),
        "nlink": info.st_nlink,
        "dev": info.st_dev,
        "ino": info.st_ino,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
    }
    if kind == "file":
        content, _ = read_regular_file(
            path, label, max_bytes=MAX_CLEANUP_TREE_BYTES, allow_nlink=None
        )
        record["sha256"] = sha256_bytes(content)
    return record


def validate_identity_record(
    path: Path, record: dict[str, Any], label: str, *, stable_only: bool
) -> None:
    required = {"relative_path", "kind", "mode", "uid", "nlink", "dev", "ino", "size", "mtime_ns"}
    if record.get("kind") == "file":
        required.add("sha256")
    require_exact_keys(record, required, label)
    try:
        info = path.lstat()
    except FileNotFoundError:
        fail(f"{label} is missing")
    if stat.S_ISLNK(info.st_mode):
        fail(f"{label} must not be a symlink")
    kind = (
        "directory"
        if stat.S_ISDIR(info.st_mode)
        else "file"
        if stat.S_ISREG(info.st_mode)
        else "other"
    )
    if kind != record["kind"]:
        fail(f"{label} kind changed")
    if identity_of(info) != (record["dev"], record["ino"]):
        fail(f"{label} identity changed")
    if owner_of(info) != record["uid"] or mode_of(info) != record["mode"]:
        fail(f"{label} ownership or mode changed")
    if stable_only:
        return
    if (
        info.st_nlink != record["nlink"]
        or info.st_size != record["size"]
        or info.st_mtime_ns != record["mtime_ns"]
    ):
        fail(f"{label} metadata changed")
    if record["kind"] == "file":
        content, _ = read_regular_file(
            path, label, max_bytes=MAX_CLEANUP_TREE_BYTES, allow_nlink=None
        )
        if sha256_bytes(content) != record["sha256"]:
            fail(f"{label} content changed")


def restore_parent_metadata(path: Path, record: dict[str, Any]) -> None:
    if not path_exists_no_follow(path):
        return
    with contextlib.suppress(OSError):
        os.chmod(path, int(record["mode"]))
    with contextlib.suppress(OSError):
        os.utime(path, ns=(int(record["mtime_ns"]), int(record["mtime_ns"])))


def validate_cleanup_relative(relative: str) -> Path:
    path = normalize_relative_path(relative, "cleanup")
    if len(path.parts) > 4 or any(len(part) > 96 for part in path.parts):
        fail("cleanup relative path exceeds bound")
    if path.parts[0] not in {"tombstones"}:
        fail("cleanup relative path is outside the manager tombstone namespace")
    return path


def validate_cleanup_records(
    root: Path, records: list[dict[str, Any]], *, allow_partial: bool
) -> None:
    if not isinstance(records, list) or not records:
        fail("cleanup record list is malformed")
    by_relative: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            fail("cleanup record entry is malformed")
        required = {
            "relative_path",
            "kind",
            "mode",
            "uid",
            "nlink",
            "dev",
            "ino",
            "size",
            "mtime_ns",
        }
        if record.get("kind") == "file":
            required.add("sha256")
        require_exact_keys(record, required, "cleanup tree record")
        relative = record["relative_path"]
        if not isinstance(relative, str) or relative in by_relative:
            fail("cleanup tree has duplicate or malformed relative path")
        by_relative[relative] = record
    if "." not in by_relative:
        fail("cleanup tree missing root record")
    for relative, record in by_relative.items():
        path = root if relative == "." else root / relative
        if not path_exists_no_follow(path):
            if allow_partial:
                continue
            fail(f"cleanup tombstone object is missing: {relative}")
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            fail(f"cleanup tombstone contains symlink: {relative}")
        if identity_of(info) != (record["dev"], record["ino"]):
            fail(f"cleanup tombstone object identity changed: {relative}")
        if owner_of(info) != record["uid"] or mode_of(info) != record["mode"]:
            fail(f"cleanup tombstone object ownership or mode changed: {relative}")
        if record["kind"] == "file":
            content, _ = read_regular_file(
                path, f"cleanup tombstone file {relative}", max_bytes=MAX_CLEANUP_TREE_BYTES
            )
            if info.st_size != record["size"] or sha256_bytes(content) != record["sha256"]:
                fail(f"cleanup tombstone file content changed: {relative}")
        elif record["kind"] == "directory":
            if not stat.S_ISDIR(info.st_mode):
                fail(f"cleanup tombstone directory kind changed: {relative}")
            declared_children = {
                key.split("/", 1)[0]
                if relative == "."
                else key[len(relative) + 1 :].split("/", 1)[0]
                for key in by_relative
                if key != relative and (relative == "." or key.startswith(relative + "/"))
            }
            try:
                current_children = {child.name for child in path.iterdir()}
            except FileNotFoundError:
                if allow_partial:
                    continue
                raise
            unknown = current_children - declared_children
            if unknown:
                fail(f"cleanup tombstone has unknown children: {sorted(unknown)}")
        else:
            fail("cleanup tombstone contains unsupported object kind")


def cleanup_journal_allowed_relatives(journal: dict[str, Any]) -> set[str]:
    allowed = {
        CLEANUP_JOURNAL_NAME,
        CLEANUP_INTENT_NAME,
        CLEANUP_PREPARE_INTENT_NAME,
        CLEANUP_RETIRED_NAME,
    }
    for entry in journal["entries"]:
        root_relative = validate_cleanup_relative(entry["relative_path"])
        current = Path()
        for part in root_relative.parts:
            current = current / part
            allowed.add(current.as_posix())
        for record in entry["records"]:
            if record["relative_path"] == ".":
                continue
            record_relative = normalize_relative_path(record["relative_path"], "cleanup record")
            current = root_relative
            for part in record_relative.parts:
                current = current / part
                allowed.add(current.as_posix())
    return allowed


def remove_empty_cleanup_ancestors(path: Path, stop: Path) -> None:
    current = path
    while current != stop and current.is_relative_to(stop):
        if not path_exists_no_follow(current):
            current = current.parent
            continue
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            fail("cleanup source ancestor has unsafe kind")
        try:
            current.rmdir()
        except OSError:
            if any(current.iterdir()):
                return
            raise
        fault("cleanup-source-empty-dir-rmdir")
        fsync_parent(current)
        fault("cleanup-source-empty-dir-parent-fsync")
        current = current.parent


def validate_prepare_work_relative(relative: str) -> Path:
    path = validate_bounded_relative(relative, "prepare hold", first_pattern=WORK_ROOT_PATTERN)
    if len(path.parts) < 3:
        fail("prepare hold path is too shallow")
    return path


def validate_prepare_work_root_relative(relative: str) -> Path:
    path = validate_bounded_relative(relative, "prepare work root", first_pattern=WORK_ROOT_PATTERN)
    if len(path.parts) != 1:
        fail("prepare work root path is malformed")
    return path


def validate_cleanup_source_relative(relative: str) -> Path:
    path = validate_bounded_relative(relative, "cleanup source")
    if not (
        WORK_ROOT_PATTERN.fullmatch(path.parts[0]) or STAGE_ROOT_PATTERN.fullmatch(path.parts[0])
    ):
        fail("cleanup source path is outside the declared manager namespace")
    return path


def cleanup_source_relative_to_parent(parent: Path, source: Path) -> Path:
    try:
        info = source.lstat()
    except FileNotFoundError:
        fail("cleanup source disappeared during binding")
    if stat.S_ISLNK(info.st_mode):
        fail("cleanup source must not be a symlink")
    try:
        return validate_cleanup_source_relative(source.relative_to(parent).as_posix())
    except ValueError:
        pass
    try:
        resolved_parent = parent.resolve(strict=True)
        resolved_source = source.resolve(strict=True)
    except OSError as exc:
        fail(f"cleanup source cannot be resolved under its canonical parent: {exc}")
    try:
        return validate_cleanup_source_relative(
            resolved_source.relative_to(resolved_parent).as_posix()
        )
    except ValueError:
        fail("cleanup source is outside the fixed cleanup parent")
    raise AssertionError("unreachable")


def validate_prepare_source_relative(relative: str) -> Path:
    path = validate_bounded_relative(relative, "prepare source")
    if path.parts[0] == CLEANUP_DIR_NAME:
        fail("prepare source path must be active target state")
    return path


def validate_prepare_intent(target: Path, intent: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "product_name",
        "build_version",
        "canonical_target",
        "cleanup_parent",
        "transaction_id",
        "work_relative_path",
        "target_root",
        "entry_count",
        "entries",
    }
    require_exact_keys(intent, required, "cleanup prepare intent")
    if (
        intent["schema_version"] != 1
        or intent["product_name"] != PRODUCT_NAME
        or intent["build_version"] != VERSION
        or intent["canonical_target"] != str(target)
        or intent["cleanup_parent"] != CLEANUP_DIR_NAME
    ):
        fail("cleanup prepare intent is not bound to this target")
    work_relative = validate_prepare_work_root_relative(intent["work_relative_path"])
    validate_identity_record(
        target, intent["target_root"], "cleanup prepare target root", stable_only=True
    )
    entries = intent["entries"]
    if not isinstance(entries, list) or not 0 < len(entries) <= MAX_CLEANUP_ENTRIES:
        fail("cleanup prepare intent entry list is malformed")
    if intent["entry_count"] != len(entries):
        fail("cleanup prepare intent entry_count mismatch")
    seen_sources: set[str] = set()
    seen_holds: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            fail("cleanup prepare intent entry is malformed")
        require_exact_keys(
            entry,
            {
                "source_anchor",
                "source_relative_path",
                "hold_anchor",
                "hold_relative_path",
                "source_kind",
                "source_parent",
                "hold_parent",
                "source_tree_sha256",
                "source_records",
            },
            "cleanup prepare intent entry",
        )
        if entry["source_anchor"] != "target" or entry["hold_anchor"] != CLEANUP_DIR_NAME:
            fail("cleanup prepare intent anchors are malformed")
        if entry["source_kind"] not in {"managed-undo", "software-undo", "backup-undo"}:
            fail("cleanup prepare intent source kind is unsupported")
        source_relative = validate_prepare_source_relative(entry["source_relative_path"])
        hold_relative = validate_prepare_work_relative(entry["hold_relative_path"])
        if hold_relative.parts[0] != work_relative.parts[0]:
            fail("cleanup prepare intent hold path is outside its work root")
        source_key = source_relative.as_posix()
        hold_key = hold_relative.as_posix()
        if source_key in seen_sources or hold_key in seen_holds:
            fail("cleanup prepare intent contains duplicate paths")
        seen_sources.add(source_key)
        seen_holds.add(hold_key)
        records = entry["source_records"]
        if cleanup_tree_sha256(records) != entry["source_tree_sha256"]:
            fail("cleanup prepare intent tree digest mismatch")
        source = target / source_relative
        hold = cleanup_parent(target) / hold_relative
        source_present = path_exists_no_follow(source)
        hold_present = path_exists_no_follow(hold)
        if source_present and hold_present:
            fail("cleanup prepare intent has both source and hold visible")
        if not source_present and not hold_present:
            fail("cleanup prepare intent lost both source and hold")
        if source_present:
            validate_cleanup_records(source, records, allow_partial=False)
            validate_identity_record(
                source.parent,
                entry["source_parent"],
                "cleanup prepare source parent",
                stable_only=True,
            )
        if hold_present:
            validate_cleanup_records(hold, records, allow_partial=False)
            validate_identity_record(
                hold.parent, entry["hold_parent"], "cleanup prepare hold parent", stable_only=True
            )


def load_prepare_intent(target: Path, *, read_only: bool) -> Optional[dict[str, Any]]:
    path = cleanup_prepare_intent_path(target)
    if not path_exists_no_follow(path):
        return None
    if not read_only:
        recover_hardlink_publication_alias(
            path,
            temp_pattern=PREPARE_TEMP_PATTERN,
            label="cleanup prepare intent",
        )
    intent = load_strict_json_file(path, "cleanup prepare intent", read_only=read_only)
    validate_prepare_intent(target, intent)
    return intent


def remove_empty_prepare_work_root(target: Path, intent: dict[str, Any]) -> None:
    root = cleanup_parent(target) / validate_prepare_work_root_relative(
        intent["work_relative_path"]
    )
    if not path_exists_no_follow(root):
        return
    for path in sorted([root, *root.rglob("*")], key=lambda item: len(item.parts), reverse=True):
        if not path_exists_no_follow(path):
            continue
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            try:
                path.rmdir()
            except OSError as exc:
                fail(f"cleanup prepare work root is not empty: {exc}")
            fsync_parent(path)
        else:
            fail("cleanup prepare work root contains unknown residue")


def recover_prepare_intent_before_mutation(target: Path) -> bool:
    intent = load_prepare_intent(target, read_only=False)
    if intent is None:
        return False
    parent_restores: list[tuple[Path, dict[str, Any]]] = []
    for entry in reversed(intent["entries"]):
        source = target / validate_prepare_source_relative(entry["source_relative_path"])
        hold = cleanup_parent(target) / validate_prepare_work_relative(entry["hold_relative_path"])
        parent_restores.append((source.parent, entry["source_parent"]))
        if path_exists_no_follow(hold):
            if path_exists_no_follow(source):
                fail("cleanup prepare recovery found both source and hold")
            ensure_private_parent(source, f"cleanup prepare restore {source}")
            os.replace(hold, source)
            fault("cleanup-prepare-restore-move")
            fsync_parent(source)
            fault("cleanup-prepare-restore-source-parent-fsync")
            fsync_parent(hold)
            fault("cleanup-prepare-restore-hold-parent-fsync")
        elif not path_exists_no_follow(source):
            fail("cleanup prepare recovery lost both source and hold")
        validate_cleanup_records(source, entry["source_records"], allow_partial=False)
    remove_empty_prepare_work_root(target, intent)
    cleanup_prepare_intent_path(target).unlink()
    fault("cleanup-prepare-intent-unlink")
    fsync_parent(cleanup_prepare_intent_path(target))
    fault("cleanup-prepare-intent-parent-fsync")
    if path_exists_no_follow(cleanup_parent(target)) and not any(cleanup_parent(target).iterdir()):
        cleanup_parent(target).rmdir()
        fsync_parent(cleanup_parent(target))
    for parent, record in parent_restores:
        restore_parent_metadata(parent, record)
    restore_parent_metadata(target, intent["target_root"])
    return True


def validate_cleanup_intent(target: Path, intent: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "product_name",
        "build_version",
        "canonical_target",
        "cleanup_parent",
        "transaction_id",
        "entry_count",
        "entries",
    }
    require_exact_keys(intent, required, "cleanup promotion intent")
    if (
        intent["schema_version"] != 1
        or intent["product_name"] != PRODUCT_NAME
        or intent["build_version"] != VERSION
        or intent["canonical_target"] != str(target)
        or intent["cleanup_parent"] != CLEANUP_DIR_NAME
    ):
        fail("cleanup promotion intent is not bound to this target")
    entries = intent["entries"]
    if not isinstance(entries, list) or not 0 < len(entries) <= MAX_CLEANUP_ENTRIES:
        fail("cleanup promotion intent entry list is malformed")
    if intent["entry_count"] != len(entries):
        fail("cleanup promotion intent entry_count mismatch")
    seen_sources: set[str] = set()
    seen_destinations: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            fail("cleanup promotion intent entry is malformed")
        require_exact_keys(
            entry,
            {
                "source_anchor",
                "source_relative_path",
                "destination_relative_path",
                "source_kind",
                "source_parent",
                "destination_parent",
                "source_tree_sha256",
                "source_records",
            },
            "cleanup promotion intent entry",
        )
        if entry["source_anchor"] != CLEANUP_DIR_NAME:
            fail("cleanup promotion intent source anchor is malformed")
        if entry["source_kind"] not in {
            "managed-undo",
            "software-undo",
            "backup-undo",
            "stage-undo",
        }:
            fail("cleanup promotion intent source kind is unsupported")
        source_relative = validate_cleanup_source_relative(entry["source_relative_path"])
        destination_relative = validate_cleanup_relative(entry["destination_relative_path"])
        source_key = source_relative.as_posix()
        destination_key = destination_relative.as_posix()
        if source_key in seen_sources or destination_key in seen_destinations:
            fail("cleanup promotion intent contains duplicate paths")
        seen_sources.add(source_key)
        seen_destinations.add(destination_key)
        records = entry["source_records"]
        if cleanup_tree_sha256(records) != entry["source_tree_sha256"]:
            fail("cleanup promotion intent tree digest mismatch")
        source = cleanup_parent(target) / source_relative
        destination = cleanup_parent(target) / destination_relative
        source_present = path_exists_no_follow(source)
        destination_present = path_exists_no_follow(destination)
        if source_present and destination_present:
            fail("cleanup promotion intent has both source and tombstone visible")
        if not source_present and not destination_present:
            fail("cleanup promotion intent lost both source and tombstone")
        if source_present:
            validate_cleanup_records(source, records, allow_partial=False)
            validate_identity_record(
                source.parent,
                entry["source_parent"],
                "cleanup promotion source parent",
                stable_only=True,
            )
        if destination_present:
            validate_cleanup_records(destination, records, allow_partial=False)
            validate_identity_record(
                destination.parent,
                entry["destination_parent"],
                "cleanup promotion destination parent",
                stable_only=True,
            )


def load_cleanup_intent(target: Path, *, read_only: bool) -> Optional[dict[str, Any]]:
    path = cleanup_intent_path(target)
    if not path_exists_no_follow(path):
        return None
    if not read_only:
        recover_hardlink_publication_alias(
            path,
            temp_pattern=CLEANUP_INTENT_TEMP_PATTERN,
            label="cleanup promotion intent",
        )
    intent = load_strict_json_file(path, "cleanup promotion intent", read_only=read_only)
    validate_cleanup_intent(target, intent)
    return intent


def journal_from_cleanup_intent(target: Path, intent: dict[str, Any]) -> dict[str, Any]:
    entries = [
        {
            "relative_path": validate_cleanup_relative(
                entry["destination_relative_path"]
            ).as_posix(),
            "source_kind": entry["source_kind"],
            "tree_sha256": entry["source_tree_sha256"],
            "records": entry["source_records"],
        }
        for entry in intent["entries"]
    ]
    journal = {
        "schema_version": 1,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "canonical_target": str(target),
        "cleanup_parent": CLEANUP_DIR_NAME,
        "transaction_id": intent["transaction_id"],
        "entry_count": len(entries),
        "entries": entries,
    }
    validate_cleanup_journal(target, journal)
    return journal


def recover_cleanup_intent_before_mutation(target: Path) -> bool:
    if path_exists_no_follow(cleanup_journal_path(target)):
        return False
    intent = load_cleanup_intent(target, read_only=False)
    if intent is None:
        return False
    for entry in intent["entries"]:
        source = cleanup_parent(target) / validate_cleanup_source_relative(
            entry["source_relative_path"]
        )
        destination = cleanup_parent(target) / validate_cleanup_relative(
            entry["destination_relative_path"]
        )
        if path_exists_no_follow(source):
            if path_exists_no_follow(destination):
                fail("cleanup promotion recovery found both source and tombstone")
            ensure_private_parent(destination, f"cleanup promotion tombstone {destination}")
            os.replace(source, destination)
            fault("cleanup-promotion-recover-move")
            fsync_parent(source)
            fault("cleanup-promotion-recover-source-parent-fsync")
            fsync_parent(destination)
            fault("cleanup-promotion-recover-destination-parent-fsync")
            remove_empty_cleanup_ancestors(source.parent, cleanup_parent(target))
        elif not path_exists_no_follow(destination):
            fail("cleanup promotion recovery lost both source and tombstone")
        validate_cleanup_records(destination, entry["source_records"], allow_partial=False)
    journal = journal_from_cleanup_intent(target, intent)
    created = publish_no_replace_file(
        cleanup_journal_path(target),
        canonical_json(journal),
        temp_pattern=JOURNAL_TEMP_PATTERN,
        label="cleanup-journal",
    )
    if not created:
        fail("cleanup journal appeared during promotion recovery")
    return True


def load_cleanup_journal(target: Path, *, read_only: bool) -> Optional[dict[str, Any]]:
    journal_path = cleanup_journal_path(target)
    intent_path = cleanup_intent_path(target)
    if path_exists_no_follow(intent_path) and not path_exists_no_follow(journal_path):
        fail("cleanup preparation intent exists without a final journal")
    if not path_exists_no_follow(journal_path):
        return None
    if not read_only:
        recover_hardlink_publication_alias(
            journal_path,
            temp_pattern=JOURNAL_TEMP_PATTERN,
            label="cleanup journal",
        )
    journal = load_strict_json_file(journal_path, "cleanup journal", read_only=read_only)
    validate_cleanup_journal(target, journal)
    return journal


def validate_visible_cleanup_journal_after_publication(target: Path) -> None:
    journal_path = cleanup_journal_path(target)
    content, _info = read_regular_file(
        journal_path,
        "cleanup journal",
        owner_only=True,
        max_bytes=MAX_CLEANUP_JOURNAL_BYTES,
        allow_nlink=None,
    )
    journal = parse_json_object(content, "cleanup journal")
    validate_cleanup_journal(target, journal, allow_publication_aliases=True)


def validate_visible_cleanup_intent_after_publication(target: Path) -> None:
    intent_path = cleanup_intent_path(target)
    content, _info = read_regular_file(
        intent_path,
        "cleanup promotion intent",
        owner_only=True,
        max_bytes=MAX_CLEANUP_JOURNAL_BYTES,
        allow_nlink=None,
    )
    intent = parse_json_object(content, "cleanup promotion intent")
    validate_cleanup_intent(target, intent)


def validate_cleanup_journal(
    target: Path,
    journal: dict[str, Any],
    *,
    allow_publication_aliases: bool = False,
) -> None:
    required = {
        "schema_version",
        "product_name",
        "build_version",
        "canonical_target",
        "cleanup_parent",
        "transaction_id",
        "entry_count",
        "entries",
    }
    require_exact_keys(journal, required, "cleanup journal")
    if (
        journal["schema_version"] != 1
        or journal["product_name"] != PRODUCT_NAME
        or journal["build_version"] != VERSION
        or journal["canonical_target"] != str(target)
        or journal["cleanup_parent"] != CLEANUP_DIR_NAME
    ):
        fail("cleanup journal is not bound to this target")
    entries = journal["entries"]
    if not isinstance(entries, list) or not 0 < len(entries) <= MAX_CLEANUP_ENTRIES:
        fail("cleanup journal entry list is malformed")
    if journal["entry_count"] != len(entries):
        fail("cleanup journal entry_count mismatch")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            fail("cleanup journal entry is malformed")
        require_exact_keys(
            entry,
            {"relative_path", "source_kind", "tree_sha256", "records"},
            "cleanup journal entry",
        )
        relative = validate_cleanup_relative(entry["relative_path"])
        if relative.as_posix() in seen:
            fail("cleanup journal has duplicate relative path")
        seen.add(relative.as_posix())
        if entry["source_kind"] not in {
            "managed-undo",
            "software-undo",
            "backup-undo",
            "stage-undo",
        }:
            fail("cleanup journal source kind is unsupported")
        records = entry["records"]
        if cleanup_tree_sha256(records) != entry["tree_sha256"]:
            fail("cleanup journal tree digest mismatch")
        validate_cleanup_records(cleanup_parent(target) / relative, records, allow_partial=True)
    if path_exists_no_follow(cleanup_parent(target)):
        allowed_relatives = cleanup_journal_allowed_relatives(journal)
        for child in cleanup_parent(target).iterdir():
            if JOURNAL_TEMP_PATTERN.fullmatch(child.name) and allow_publication_aliases:
                continue
            if child.name.startswith(".NDDEV-QODER-CLI-CLEANUP.json.pubtmp."):
                fail("cleanup journal publication alias requires mutation recovery")
            if child.name.startswith(".NDDEV-QODER-CLI-CLEANUP-INTENT.json.pubtmp."):
                fail("cleanup intent publication alias requires mutation recovery")
            if child.name.startswith(".NDDEV-QODER-CLI-CLEANUP-RETIRED.json.pubtmp."):
                fail("cleanup retired publication alias requires mutation recovery")
        for path in cleanup_parent(target).rglob("*"):
            relative = path.relative_to(cleanup_parent(target)).as_posix()
            if JOURNAL_TEMP_PATTERN.fullmatch(path.name) and allow_publication_aliases:
                continue
            if relative not in allowed_relatives:
                fail(f"cleanup directory contains unknown state: {relative}")


def build_cleanup_retired_marker(target: Path, journal: dict[str, Any]) -> dict[str, Any]:
    marker = {
        "schema_version": 1,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "canonical_target": str(target),
        "cleanup_parent": CLEANUP_DIR_NAME,
        "transaction_id": journal["transaction_id"],
        "entry_count": journal["entry_count"],
        "journal_sha256": sha256_bytes(canonical_json(journal)),
    }
    validate_cleanup_retired_marker(target, marker)
    return marker


def validate_cleanup_retired_marker(target: Path, marker: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "product_name",
        "build_version",
        "canonical_target",
        "cleanup_parent",
        "transaction_id",
        "entry_count",
        "journal_sha256",
    }
    require_exact_keys(marker, required, "cleanup retired marker")
    if (
        marker["schema_version"] != 1
        or marker["product_name"] != PRODUCT_NAME
        or marker["build_version"] != VERSION
        or marker["canonical_target"] != str(target)
        or marker["cleanup_parent"] != CLEANUP_DIR_NAME
        or not isinstance(marker["transaction_id"], str)
        or not isinstance(marker["entry_count"], int)
        or not isinstance(marker["journal_sha256"], str)
        or not re.fullmatch(r"[a-f0-9]{64}", marker["journal_sha256"])
    ):
        fail("cleanup retired marker is malformed or mismatched")


def load_cleanup_retired_marker(target: Path, *, read_only: bool) -> Optional[dict[str, Any]]:
    path = cleanup_retired_path(target)
    if not path_exists_no_follow(path):
        return None
    if not read_only:
        recover_hardlink_publication_alias(
            path,
            temp_pattern=RETIRED_TEMP_PATTERN,
            label="cleanup retired marker",
        )
    marker = load_strict_json_file(path, "cleanup retired marker", read_only=read_only)
    validate_cleanup_retired_marker(target, marker)
    return marker


def cleanup_pending_state(target: Path, *, read_only: bool) -> tuple[bool, dict[str, Any]]:
    if not path_exists_no_follow(target):
        return False, {"cleanup_pending": False}
    if not path_exists_no_follow(cleanup_parent(target)):
        return False, {"cleanup_pending": False}
    require_private_directory(cleanup_parent(target), "cleanup parent")
    if path_exists_no_follow(cleanup_prepare_intent_path(target)) and not path_exists_no_follow(
        cleanup_journal_path(target)
    ):
        fail("cleanup prepare intent exists without a final journal")
    retired = load_cleanup_retired_marker(target, read_only=read_only)
    if retired is not None and not path_exists_no_follow(cleanup_journal_path(target)):
        return True, {
            "cleanup_pending": True,
            "cleanup": {
                "entry_count": retired["entry_count"],
                "transaction_id": retired["transaction_id"],
                "phase": "retired",
            },
        }
    journal = load_cleanup_journal(target, read_only=read_only)
    if journal is None:
        children = sorted(child.name for child in cleanup_parent(target).iterdir())
        if children:
            fail("cleanup directory contains uncommitted state")
        fail("cleanup directory exists without cleanup authority")
    return True, {
        "cleanup_pending": True,
        "cleanup": {
            "entry_count": journal["entry_count"],
            "transaction_id": journal["transaction_id"],
        },
    }


def delete_cleanup_entry(target: Path, entry: dict[str, Any]) -> None:
    root = cleanup_parent(target) / validate_cleanup_relative(entry["relative_path"])
    records = entry["records"]
    validate_cleanup_records(root, records, allow_partial=True)
    for record in sorted(
        records,
        key=lambda item: (item["relative_path"].count("/"), item["relative_path"]),
        reverse=True,
    ):
        relative = record["relative_path"]
        path = root if relative == "." else root / relative
        if not path_exists_no_follow(path):
            continue
        info = path.lstat()
        if record["kind"] == "file":
            if identity_of(info) != (record["dev"], record["ino"]):
                fail(f"cleanup tombstone object identity changed: {relative}")
            content, _ = read_regular_file(
                path, f"cleanup file {relative}", max_bytes=MAX_CLEANUP_TREE_BYTES
            )
            if sha256_bytes(content) != record["sha256"]:
                fail(f"cleanup tombstone file content changed: {relative}")
            path.unlink()
            fault("cleanup-unlink")
            fsync_parent(path)
            fault("cleanup-unlink-parent-fsync")
        elif record["kind"] == "directory":
            if identity_of(info) != (record["dev"], record["ino"]):
                fail(f"cleanup tombstone object identity changed: {relative}")
            unknown = set(child.name for child in path.iterdir())
            if unknown:
                declared_remaining = set()
                for other in records:
                    other_rel = other["relative_path"]
                    if other_rel == relative or not path_exists_no_follow(
                        root if other_rel == "." else root / other_rel
                    ):
                        continue
                    if relative == ".":
                        declared_remaining.add(other_rel.split("/", 1)[0])
                    elif other_rel.startswith(relative + "/"):
                        declared_remaining.add(other_rel[len(relative) + 1 :].split("/", 1)[0])
                if unknown - declared_remaining:
                    fail(
                        f"cleanup tombstone has unknown children before rmdir: {sorted(unknown - declared_remaining)}"
                    )
                if unknown:
                    continue
            path.rmdir()
            fault("cleanup-rmdir")
            fsync_parent(path)
            fault("cleanup-rmdir-parent-fsync")
        else:
            fail("cleanup tombstone contains unsupported object kind")


def remove_empty_cleanup_dirs(target: Path) -> None:
    parent = cleanup_parent(target)
    if not path_exists_no_follow(parent):
        return
    for path in sorted(
        [path for path in parent.rglob("*") if path.is_dir() and not path.is_symlink()],
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        try:
            path.rmdir()
        except OSError:
            continue
        fsync_parent(path)
    parent.rmdir()
    fsync_parent(parent)


def delete_bounded_uncommitted_tree(path: Path) -> None:
    if not path_exists_no_follow(path):
        return
    first = path.name
    if not (WORK_ROOT_PATTERN.fullmatch(first) or STAGE_ROOT_PATTERN.fullmatch(first)):
        fail(f"uncommitted cleanup residue is outside manager namespace: {first}")
    for entry in sorted([path, *path.rglob("*")], key=lambda item: len(item.parts), reverse=True):
        if not path_exists_no_follow(entry):
            continue
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode):
            fail("uncommitted cleanup residue contains a symlink")
        if stat.S_ISDIR(info.st_mode):
            if not is_owner_private_directory(info):
                fail("uncommitted cleanup residue directory is not private")
            entry.rmdir()
        elif stat.S_ISREG(info.st_mode):
            if owner_of(info) != current_uid():
                fail("uncommitted cleanup residue file owner is invalid")
            entry.unlink()
        else:
            fail("uncommitted cleanup residue contains unsupported object")
        fsync_parent(entry)


def recover_uncommitted_cleanup_residue_before_mutation(target: Path) -> bool:
    parent = cleanup_parent(target)
    if not path_exists_no_follow(parent):
        return False
    authority_paths = (
        cleanup_prepare_intent_path(target),
        cleanup_intent_path(target),
        cleanup_journal_path(target),
        cleanup_retired_path(target),
    )
    if any(path_exists_no_follow(path) for path in authority_paths):
        return False
    require_private_directory(parent, "cleanup parent")
    children = sorted(parent.iterdir(), key=lambda item: item.name)
    if not children:
        parent.rmdir()
        fsync_parent(parent)
        return True
    for child in children:
        delete_bounded_uncommitted_tree(child)
    parent.rmdir()
    fsync_parent(parent)
    return True


def finalize_retired_cleanup(target: Path) -> bool:
    marker = load_cleanup_retired_marker(target, read_only=False)
    if marker is None or path_exists_no_follow(cleanup_journal_path(target)):
        return False
    parent = cleanup_parent(target)
    if path_exists_no_follow(parent):
        for directory in sorted(
            [path for path in parent.rglob("*") if path.is_dir() and not path.is_symlink()],
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            with contextlib.suppress(OSError):
                directory.rmdir()
                fsync_parent(directory)
    for path, name in (
        (cleanup_intent_path(target), "cleanup-intent"),
        (cleanup_prepare_intent_path(target), "cleanup-prepare-intent"),
    ):
        if path_exists_no_follow(path):
            path.unlink()
            fault(f"{name}-unlink")
            fsync_parent(path)
            fault(f"{name}-parent-fsync")
    if path_exists_no_follow(parent):
        for directory in sorted(
            [path for path in parent.rglob("*") if path.is_dir() and not path.is_symlink()],
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            with contextlib.suppress(OSError):
                directory.rmdir()
                fsync_parent(directory)
        children = sorted(
            child.name for child in parent.iterdir() if child != cleanup_retired_path(target)
        )
        if children:
            fail(f"cleanup retired state has unexpected residue: {children}")
    cleanup_retired_path(target).unlink()
    fault("cleanup-retired-marker-unlink")
    fsync_parent(cleanup_retired_path(target))
    fault("cleanup-retired-marker-parent-fsync")
    if path_exists_no_follow(parent):
        try:
            parent.rmdir()
            fsync_parent(parent)
        except OSError:
            pass
    return True


def drain_cleanup_before_mutation(target: Path) -> bool:
    if not path_exists_no_follow(target):
        return False
    if not path_exists_no_follow(cleanup_parent(target)):
        return False
    recovered = False
    if path_exists_no_follow(cleanup_intent_path(target)) and not path_exists_no_follow(
        cleanup_journal_path(target)
    ):
        recovered = recover_cleanup_intent_before_mutation(target)
    elif not path_exists_no_follow(cleanup_journal_path(target)):
        recovered = recover_prepare_intent_before_mutation(target)
    if finalize_retired_cleanup(target):
        return True
    if not path_exists_no_follow(cleanup_parent(target)):
        return recovered
    recovered = recover_uncommitted_cleanup_residue_before_mutation(target) or recovered
    if not path_exists_no_follow(cleanup_parent(target)):
        return recovered
    pending, details = cleanup_pending_state(target, read_only=False)
    if not pending:
        return recovered
    journal = load_cleanup_journal(target, read_only=False)
    assert journal is not None
    for entry in journal["entries"]:
        delete_cleanup_entry(target, entry)
    if not path_exists_no_follow(cleanup_retired_path(target)):
        marker = build_cleanup_retired_marker(target, journal)
        publish_no_replace_file(
            cleanup_retired_path(target),
            canonical_json(marker),
            temp_pattern=RETIRED_TEMP_PATTERN,
            label="cleanup-retired",
        )
    cleanup_journal_path(target).unlink()
    fault("cleanup-journal-unlink")
    fsync_parent(cleanup_journal_path(target))
    fault("cleanup-journal-parent-fsync")
    finalize_retired_cleanup(target)
    return bool(details["cleanup_pending"])


def publish_cleanup_journal(target: Path, sources: list[tuple[Path, str]]) -> bool:
    parent = cleanup_parent(target)
    live_sources: list[tuple[Path, str, Path]] = []
    for source, kind in sources:
        if not path_exists_no_follow(source):
            continue
        source_relative = cleanup_source_relative_to_parent(parent, source)
        live_sources.append((parent / source_relative, kind, source_relative))
    if not live_sources:
        return False
    parent_creation = PrivateDirectoryCreation()
    parent_creation.ensure(parent, "cleanup parent")
    if path_exists_no_follow(cleanup_journal_path(target)) or path_exists_no_follow(
        cleanup_intent_path(target)
    ):
        fail("cleanup journal already exists")
    transaction_id = uuid.uuid4().hex
    tombstone_parent = parent / "tombstones" / transaction_id
    parent_creation.ensure(tombstone_parent.parent, "cleanup tombstone parent")
    intent_entries: list[dict[str, Any]] = []
    for index, (source, kind, source_relative) in enumerate(live_sources):
        if kind not in {"managed-undo", "software-undo", "backup-undo", "stage-undo"}:
            fail(f"unsupported cleanup source kind: {kind}")
        destination = tombstone_parent / f"{index}-{kind}"
        parent_creation.ensure(destination.parent, f"cleanup tombstone parent {destination.parent}")
        records = tree_records(source)
        intent_entries.append(
            {
                "source_anchor": CLEANUP_DIR_NAME,
                "source_relative_path": source_relative.as_posix(),
                "destination_relative_path": destination.relative_to(parent).as_posix(),
                "source_kind": kind,
                "source_parent": object_identity_record(
                    source.parent, parent, "cleanup promotion source parent"
                ),
                "destination_parent": object_identity_record(
                    destination.parent,
                    parent,
                    "cleanup promotion destination parent",
                ),
                "source_tree_sha256": cleanup_tree_sha256(records),
                "source_records": records,
            }
        )
    intent = {
        "schema_version": 1,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "canonical_target": str(target),
        "cleanup_parent": CLEANUP_DIR_NAME,
        "transaction_id": transaction_id,
        "entry_count": len(live_sources),
        "entries": intent_entries,
    }
    validate_cleanup_intent(target, intent)
    intent_content = canonical_json(intent)
    if len(intent_content) > MAX_CLEANUP_JOURNAL_BYTES:
        fail("cleanup intent exceeds the serialized metadata bound")
    moved: list[tuple[Path, Path]] = []
    try:
        created = publish_no_replace_file(
            cleanup_intent_path(target),
            intent_content,
            temp_pattern=CLEANUP_INTENT_TEMP_PATTERN,
            label="cleanup-intent",
        )
        if not created:
            fail("cleanup intent already exists")
        for entry, (source, _kind, _source_relative) in zip(intent_entries, live_sources):
            destination = parent / validate_cleanup_relative(entry["destination_relative_path"])
            os.replace(source, destination)
            moved.append((source, destination))
            fault("cleanup-source-move")
            fsync_parent(source)
            fault("cleanup-source-parent-fsync")
            fsync_parent(destination)
            fault("cleanup-destination-parent-fsync")
            validate_cleanup_records(destination, entry["source_records"], allow_partial=False)
            remove_empty_cleanup_ancestors(source.parent, parent)
        journal = journal_from_cleanup_intent(target, intent)
        validate_cleanup_journal(target, journal)
        journal_content = canonical_json(journal)
        if len(journal_content) > MAX_CLEANUP_JOURNAL_BYTES:
            fail("cleanup journal exceeds the serialized metadata bound")
        publish_no_replace_file(
            cleanup_journal_path(target),
            journal_content,
            temp_pattern=JOURNAL_TEMP_PATTERN,
            label="cleanup-journal",
        )
    except BaseException as exc:
        if path_exists_no_follow(cleanup_journal_path(target)):
            try:
                validate_visible_cleanup_journal_after_publication(target)
            except BaseException as validation_exc:
                raise CleanupJournalPublishedInvalid(
                    f"visible cleanup journal is invalid after publication: {validation_exc}"
                ) from validation_exc
            raise CleanupFailure(f"post-commit cleanup is pending: {exc}") from exc
        if path_exists_no_follow(cleanup_intent_path(target)):
            try:
                validate_visible_cleanup_intent_after_publication(target)
            except BaseException as validation_exc:
                raise CleanupJournalPublishedInvalid(
                    f"visible cleanup intent is invalid after publication: {validation_exc}"
                ) from validation_exc
            raise CleanupFailure(f"post-commit cleanup is pending: {exc}") from exc
        for source, destination in reversed(moved):
            with contextlib.suppress(FileNotFoundError):
                os.replace(destination, source)
                fsync_parent(source)
        parent_creation.rollback()
        raise
    try:
        drain_cleanup_before_mutation(target)
    except BaseException as exc:
        raise CleanupFailure(f"post-commit cleanup is pending: {exc}") from exc
    return False


class UndoTransaction:
    def __init__(self, target: Path, label: str) -> None:
        self.target = target
        self.label = label
        self.transaction_id = uuid.uuid4().hex
        self.parent_records: dict[Path, dict[str, Any]] = {}
        self.created_dirs: list[Path] = []
        self.created_files: list[Path] = []
        self.moved_to_hold: list[tuple[Path, Path]] = []
        self.committed_sources: list[tuple[Path, str]] = []
        self.prepared_holds: dict[tuple[str, str], Path] = {}
        self.target_root_record = object_identity_record(target, target, "target root")
        ensure_private_directory(cleanup_parent(target))
        self.work = cleanup_parent(target) / f".work-{self.transaction_id}-{label}"
        self.work.mkdir(mode=OWNER_DIRECTORY_MODE)
        os.chmod(self.work, OWNER_DIRECTORY_MODE)
        fsync_parent(self.work)

    def snapshot_parent(self, path: Path) -> None:
        parent = path.parent
        if parent in self.parent_records:
            return
        try:
            snapshot = directory_snapshot(parent, f"{path} parent")
        except FileNotFoundError:
            return
        self.parent_records[parent] = snapshot

    def ensure_dir(self, path: Path) -> None:
        if path_exists_no_follow(path):
            require_private_directory(path, str(path))
            return
        missing: list[Path] = []
        current = path
        while not path_exists_no_follow(current):
            missing.append(current)
            current = current.parent
        require_private_directory(current, f"{path} existing ancestor")
        for directory in reversed(missing):
            self.snapshot_parent(directory)
            directory.mkdir(mode=OWNER_DIRECTORY_MODE)
            os.chmod(directory, OWNER_DIRECTORY_MODE)
            fsync_parent(directory)
            self.created_dirs.append(directory)

    def prepare_hold_moves(self, candidates: list[tuple[Path, str]]) -> list[tuple[Path, str]]:
        if self.prepared_holds:
            fail("cleanup prepare authority already exists for this transaction")
        live: list[tuple[Path, str]] = []
        entries: list[dict[str, Any]] = []
        for relative, kind in candidates:
            if relative.is_absolute() or ".." in relative.parts:
                fail(f"unsafe hold source path: {relative}")
            source = self.target / relative
            if not path_exists_no_follow(source):
                continue
            if kind not in {"managed-undo", "software-undo", "backup-undo"}:
                fail(f"unsupported hold kind: {kind}")
            destination = self.work / kind / relative
            self.ensure_dir(destination.parent)
            records = tree_records(source)
            entry = {
                "source_anchor": "target",
                "source_relative_path": relative.as_posix(),
                "hold_anchor": CLEANUP_DIR_NAME,
                "hold_relative_path": destination.relative_to(
                    cleanup_parent(self.target)
                ).as_posix(),
                "source_kind": kind,
                "source_parent": object_identity_record(
                    source.parent, self.target, "cleanup prepare source parent"
                ),
                "hold_parent": object_identity_record(
                    destination.parent, cleanup_parent(self.target), "cleanup prepare hold parent"
                ),
                "source_tree_sha256": cleanup_tree_sha256(records),
                "source_records": records,
            }
            live.append((relative, kind))
            entries.append(entry)
            self.prepared_holds[(relative.as_posix(), kind)] = destination
        if not entries:
            return []
        if path_exists_no_follow(cleanup_prepare_intent_path(self.target)):
            fail("cleanup prepare intent already exists")
        intent = {
            "schema_version": 1,
            "product_name": PRODUCT_NAME,
            "build_version": VERSION,
            "canonical_target": str(self.target),
            "cleanup_parent": CLEANUP_DIR_NAME,
            "transaction_id": self.transaction_id,
            "work_relative_path": self.work.relative_to(cleanup_parent(self.target)).as_posix(),
            "target_root": self.target_root_record,
            "entry_count": len(entries),
            "entries": entries,
        }
        validate_prepare_intent(self.target, intent)
        created = publish_no_replace_file(
            cleanup_prepare_intent_path(self.target),
            canonical_json(intent),
            temp_pattern=PREPARE_TEMP_PATTERN,
            label="cleanup-prepare-intent",
        )
        if not created:
            fail("cleanup prepare intent already exists")
        return live

    def move_existing_to_hold(self, relative: Path, *, kind: str) -> Optional[Path]:
        source = self.target / relative
        if not path_exists_no_follow(source):
            return None
        key = (relative.as_posix(), kind)
        if key not in self.prepared_holds:
            fail("hold source move was not durably prepared")
        destination = self.prepared_holds[key]
        self.snapshot_parent(source)
        self.snapshot_parent(destination)
        os.replace(source, destination)
        fault(f"{self.label}-move-source")
        fsync_parent(source)
        fsync_parent(destination)
        self.moved_to_hold.append((source, destination))
        return destination

    def write_file(self, relative: Path, content: bytes, *, executable: bool = False) -> None:
        path = self.target / relative
        self.ensure_dir(path.parent)
        self.snapshot_parent(path)
        mode = OWNER_EXECUTABLE_MODE if executable else OWNER_FILE_MODE
        temporary = path.parent / f".{path.name}.tmp.{os.getpid()}-{uuid.uuid4().hex}"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            mode,
        )
        try:
            write_all(fd, content, f"{self.label} file {relative}")
            fault(f"{self.label}-write")
            os.fchmod(fd, mode)
            fault(f"{self.label}-fchmod")
            os.fsync(fd)
            fault(f"{self.label}-file-fsync")
        finally:
            os.close(fd)
        try:
            os.replace(temporary, path)
            fault(f"{self.label}-replace")
            fsync_parent(path)
            fault(f"{self.label}-parent-fsync")
            self.created_files.append(path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
            raise

    def move_new_tree_to_active(self, source: Path, relative: Path) -> None:
        destination = self.target / relative
        self.ensure_dir(destination.parent)
        self.snapshot_parent(destination)
        os.replace(source, destination)
        fault(f"{self.label}-move-new-tree")
        fsync_parent(destination)
        self.created_files.append(destination)

    def rollback(self) -> None:
        for path in reversed(self.created_files):
            if not path_exists_no_follow(path):
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            with contextlib.suppress(Exception):
                fsync_parent(path)
        for source, destination in reversed(self.moved_to_hold):
            if path_exists_no_follow(destination):
                self.ensure_dir(source.parent)
                os.replace(destination, source)
                with contextlib.suppress(Exception):
                    fsync_parent(source)
                    fsync_parent(destination)
        for directory in reversed(self.created_dirs):
            if (
                path_exists_no_follow(directory)
                and directory.is_dir()
                and not directory.is_symlink()
            ):
                with contextlib.suppress(OSError):
                    directory.rmdir()
                    fsync_parent(directory)
        for parent, snapshot in self.parent_records.items():
            if path_exists_no_follow(parent):
                restore_directory_snapshot(parent, snapshot, f"{parent} rollback parent")
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(self.work)
            fsync_parent(self.work)
        with contextlib.suppress(FileNotFoundError):
            cleanup_prepare_intent_path(self.target).unlink()
            fsync_parent(cleanup_prepare_intent_path(self.target))
        with contextlib.suppress(OSError):
            cleanup_parent(self.target).rmdir()
            fsync_parent(cleanup_parent(self.target))

    def mark_for_cleanup(self, relative: Path, kind: str) -> None:
        source = self.work / kind / relative
        if path_exists_no_follow(source):
            self.committed_sources.append((source, kind))

    def cleanup_success(self, extra_sources: Optional[list[tuple[Path, str]]] = None) -> bool:
        with contextlib.suppress(OSError):
            self.work.rmdir()
            fsync_parent(self.work)
        sources = [*self.committed_sources, *(extra_sources or [])]
        pending = publish_cleanup_journal(self.target, sources)
        if not sources and path_exists_no_follow(cleanup_parent(self.target)):
            try:
                cleanup_parent(self.target).rmdir()
                fsync_parent(cleanup_parent(self.target))
            except OSError:
                raise CleanupFailure("post-commit cleanup is pending: empty cleanup parent remains")
        return pending


def finish_transaction(
    txn: UndoTransaction, extra_sources: Optional[list[tuple[Path, str]]] = None
) -> bool:
    try:
        return txn.cleanup_success(extra_sources)
    except (CleanupFailure, CleanupJournalPublishedInvalid):
        raise
    except BaseException:
        txn.rollback()
        raise


def inspect_target(target: Path) -> dict[str, Any]:
    if not path_exists_no_follow(target):
        return {"state": "missing", "target": str(target), "cleanup_pending": False}
    require_private_directory(target, "target")
    pending, cleanup = cleanup_pending_state(target, read_only=True)
    stamp = load_stamp(target)
    if stamp is None:
        if any_managed_path_exists(target):
            fail("unmanaged target contains nddev-managed paths")
        return {"state": "unmanaged", "target": str(target), **cleanup}
    return {
        "state": "managed",
        "target": str(target),
        "setup_id": stamp["setup_id"],
        "profile_id": stamp["profile_id"],
        "permission_mode": stamp["permission_mode"],
        "trusted_workspace": stamp["trusted_workspace"],
        "build_version": stamp["build_version"],
        "managed_files": validate_managed_files(target, stamp),
        "builder_projection": stamp["builder_projection"],
        "launch_args": stamp["launch_args"],
        **cleanup,
    }


def changed_paths(target: Path, desired: dict[Path, bytes]) -> list[str]:
    changed: list[str] = []
    for relative, content in desired.items():
        path = target / relative
        if not path_exists_no_follow(path):
            changed.append(relative.as_posix())
            continue
        actual, _ = read_regular_file(path, f"managed file {relative}", owner_only=True)
        if actual != content:
            changed.append(relative.as_posix())
    plugin_root = target / MANAGED_PLUGIN_ROOT
    if path_exists_no_follow(plugin_root):
        validate_managed_plugin_tree(target)
    return sorted(changed)


def strict_remove_empty_managed_parents(target: Path, txn: UndoTransaction) -> None:
    del target, txn


def apply_managed_state(
    target: Path,
    desired: dict[Path, bytes],
    *,
    backup_state: Optional[dict[str, Any]] = None,
) -> bool:
    txn = UndoTransaction(target, "managed")
    try:
        validate_managed_plugin_tree(target)
        candidates: list[tuple[Path, str]] = []
        new_backup_pool: Optional[Path] = None
        if backup_state is not None:
            new_backup_pool = txn.work / "backup-new"
            build_backup_pool(target, backup_state, new_backup_pool)
            if path_exists_no_follow(backup_pool(target)):
                candidates.append((Path(BACKUP_POOL_NAME), "backup-undo"))
        for relative in MANAGED_OBJECTS:
            validate_managed_destination(target, relative)
            if path_exists_no_follow(target / relative):
                candidates.append((relative, "managed-undo"))
        for relative, kind in txn.prepare_hold_moves(candidates):
            moved = txn.move_existing_to_hold(relative, kind=kind)
            if moved is not None:
                txn.mark_for_cleanup(relative, kind)
        if new_backup_pool is not None:
            txn.move_new_tree_to_active(new_backup_pool, Path(BACKUP_POOL_NAME))
            require_private_directory(backup_pool(target), "backup pool")
        for relative, content in desired.items():
            txn.write_file(relative, content)
        stamp = load_stamp(target)
        if stamp is None:
            fail("managed postcondition failed")
        validate_managed_files(target, stamp)
    except BaseException:
        txn.rollback()
        raise
    return finish_transaction(txn)


def remove_managed_state(target: Path, *, backup_state: Optional[dict[str, Any]] = None) -> bool:
    txn = UndoTransaction(target, "managed-remove")
    try:
        validate_managed_plugin_tree(target)
        candidates: list[tuple[Path, str]] = []
        new_backup_pool: Optional[Path] = None
        if backup_state is not None:
            new_backup_pool = txn.work / "backup-new"
            build_backup_pool(target, backup_state, new_backup_pool)
            if path_exists_no_follow(backup_pool(target)):
                candidates.append((Path(BACKUP_POOL_NAME), "backup-undo"))
        for relative in MANAGED_OBJECTS:
            validate_managed_destination(target, relative)
            if path_exists_no_follow(target / relative):
                candidates.append((relative, "managed-undo"))
        for relative, kind in txn.prepare_hold_moves(candidates):
            moved = txn.move_existing_to_hold(relative, kind=kind)
            if moved is not None:
                txn.mark_for_cleanup(relative, kind)
        if new_backup_pool is not None:
            txn.move_new_tree_to_active(new_backup_pool, Path(BACKUP_POOL_NAME))
            require_private_directory(backup_pool(target), "backup pool")
        strict_remove_empty_managed_parents(target, txn)
        if load_stamp(target) is not None or any_managed_path_exists(target):
            fail("managed removal postcondition failed")
    except BaseException:
        txn.rollback()
        raise
    return finish_transaction(txn)


def backup_pool(target: Path) -> Path:
    return target / BACKUP_POOL_NAME


def backup_file_records(slot_dir: Path, managed_files: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in [*managed_files, STAMP_NAME]:
        relative = normalize_relative_path(raw, "backup")
        records.append(file_record_for_path(slot_dir, relative))
    return sorted(records, key=lambda item: item["path"])


def validate_backup_topology(slot_dir: Path, envelope: dict[str, Any]) -> None:
    managed_files = envelope["managed_files"]
    expected_files = set(managed_files) | {STAMP_NAME, BACKUP_NAME}
    expected_dirs = set()
    for raw in expected_files:
        relative = normalize_relative_path(raw, "backup")
        for index in range(1, len(relative.parts)):
            expected_dirs.add(Path(*relative.parts[:index]).as_posix())
    actual_files: set[str] = set()
    actual_dirs: set[str] = set()
    for entry in sorted(slot_dir.rglob("*")):
        relative = entry.relative_to(slot_dir).as_posix()
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode):
            fail(f"backup contains symlink: {relative}")
        if stat.S_ISDIR(info.st_mode):
            actual_dirs.add(relative)
        elif stat.S_ISREG(info.st_mode):
            actual_files.add(relative)
        else:
            fail(f"backup contains unsupported object: {relative}")
    if actual_files != expected_files:
        fail("backup file topology mismatch")
    if actual_dirs != expected_dirs:
        fail("backup directory topology mismatch")
    expected_records = envelope["file_records"]
    if expected_records != backup_file_records(slot_dir, managed_files):
        fail("backup file records do not match payload")


def load_backup(target: Path, slot: int) -> tuple[dict[str, Any], dict[Path, bytes]]:
    if slot < 0 or slot >= MAX_BACKUP_SLOTS:
        fail("--backup must be between 0 and 9")
    slot_dir = backup_pool(target) / str(slot)
    require_private_directory(slot_dir, f"backup slot {slot}")
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
    if not isinstance(envelope["managed_files"], list) or not all(
        isinstance(item, str) for item in envelope["managed_files"]
    ):
        fail("backup managed_files is malformed")
    if not isinstance(envelope["file_records"], list):
        fail("backup file_records is malformed")
    validate_backup_topology(slot_dir, envelope)
    files: dict[Path, bytes] = {}
    for raw_relative in [*envelope["managed_files"], STAMP_NAME]:
        relative = normalize_relative_path(raw_relative, "backup")
        content, _ = read_regular_file(
            slot_dir / relative, f"backup file {relative}", owner_only=True
        )
        files[relative] = content
    return envelope, files


def build_backup_pool(target: Path, state: dict[str, Any], destination: Path) -> None:
    destination.mkdir(mode=OWNER_DIRECTORY_MODE)
    os.chmod(destination, OWNER_DIRECTORY_MODE)
    existing_pool = backup_pool(target)
    old_slots: list[int] = []
    if path_exists_no_follow(existing_pool):
        require_private_directory(existing_pool, "backup pool")
        for child in existing_pool.iterdir():
            if not child.name.isdigit():
                fail("backup pool contains an unknown entry")
            slot = int(child.name)
            if slot < 0 or slot >= MAX_BACKUP_SLOTS:
                fail("backup pool contains an out-of-range slot")
            old_slots.append(slot)
    for slot in sorted(old_slots):
        if slot >= MAX_BACKUP_SLOTS - 1:
            continue
        shutil.copytree(existing_pool / str(slot), destination / str(slot + 1), symlinks=False)
    slot_dir = destination / "0"
    slot_dir.mkdir(mode=OWNER_DIRECTORY_MODE)
    os.chmod(slot_dir, OWNER_DIRECTORY_MODE)
    managed_files = list(state["managed_files"])
    for raw_relative in [*managed_files, STAMP_NAME]:
        relative = normalize_relative_path(raw_relative, "backup")
        source = target / relative
        content, _ = read_regular_file(source, f"managed file {relative}", owner_only=True)
        backup_path = slot_dir / relative
        ensure_private_parent(backup_path, f"backup file {relative}")
        write_durable_file(backup_path, content)
    envelope = {
        "schema_version": 1,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "slot": 0,
        "canonical_target": str(target),
        "source_setup_id": state["setup_id"],
        "source_profile_id": state["profile_id"],
        "trusted_workspace": state["trusted_workspace"],
        "managed_files": managed_files,
        "file_records": backup_file_records(slot_dir, managed_files),
        "created_at": int(time.time()),
    }
    write_durable_file(slot_dir / BACKUP_NAME, canonical_json(envelope))
    validate_backup_topology(slot_dir, envelope)
    for slot_dir_path in sorted(destination.iterdir()):
        if not slot_dir_path.name.isdigit():
            continue
        slot = int(slot_dir_path.name)
        envelope_path = slot_dir_path / BACKUP_NAME
        old = load_json_object(envelope_path, f"backup slot {slot} envelope", owner_only=True)
        old["slot"] = slot
        old["file_records"] = backup_file_records(slot_dir_path, old["managed_files"])
        write_durable_file(envelope_path, canonical_json(old))
        validate_backup_topology(slot_dir_path, old)


def replace_backup_pool(target: Path, state: dict[str, Any]) -> bool:
    txn = UndoTransaction(target, "backup")
    new_pool = txn.work / "backup-new"
    try:
        build_backup_pool(target, state, new_pool)
        old_pool = backup_pool(target)
        if path_exists_no_follow(old_pool):
            for relative, _kind in txn.prepare_hold_moves(
                [(Path(BACKUP_POOL_NAME), "backup-undo")]
            ):
                moved = txn.move_existing_to_hold(relative, kind="backup-undo")
                if moved is not None:
                    txn.mark_for_cleanup(relative, "backup-undo")
        txn.move_new_tree_to_active(new_pool, Path(BACKUP_POOL_NAME))
        require_private_directory(backup_pool(target), "backup pool")
    except BaseException:
        txn.rollback()
        raise
    return finish_transaction(txn)


def choose_setup_and_profile(
    state: dict[str, Any],
    setup_id: Optional[str],
    profile_id: Optional[str],
    *,
    operation: str,
) -> tuple[str, str]:
    if operation == "update" and state["state"] != "managed":
        fail("setup update requires an existing managed target")
    chosen_setup = setup_id or (
        state.get("setup_id") if state.get("state") == "managed" else DEFAULT_SETUP_ID
    )
    chosen_profile = profile_id or (
        state.get("profile_id") if state.get("state") == "managed" else DEFAULT_PROFILE_ID
    )
    validate_setup_id(str(chosen_setup))
    validate_profile_id(str(chosen_profile))
    return str(chosen_setup), str(chosen_profile)


def setup_lifecycle(
    target: Path,
    *,
    setup_id: Optional[str],
    profile_id: Optional[str],
    workspace: Optional[str],
    operation: str,
) -> dict[str, Any]:
    target_parent_snapshot = directory_snapshot(target.parent, "target parent")
    target_created = ensure_target_directory(target)
    state = inspect_target(target)
    chosen_setup, chosen_profile = choose_setup_and_profile(
        state, setup_id, profile_id, operation=operation
    )
    existing_settings = read_existing_settings_if_managed(target, state)
    if workspace:
        trusted_workspace = canonical_workspace(workspace, default=target)
    elif state.get("state") == "managed":
        trusted_workspace = Path(str(state["trusted_workspace"]))
    else:
        trusted_workspace = target
    setup, profile, desired = render_setup(
        chosen_setup,
        chosen_profile,
        canonical_target=target,
        trusted_workspace=trusted_workspace,
        existing_settings=existing_settings,
    )
    changed = changed_paths(target, desired)
    backup_slot: Optional[int] = None
    cleanup_pending = False
    active_committed = False
    try:
        if changed:
            try:
                cleanup_pending = apply_managed_state(
                    target,
                    desired,
                    backup_state=state if state["state"] == "managed" else None,
                )
            except CleanupFailure:
                cleanup_pending = True
            except CleanupJournalPublishedInvalid:
                active_committed = True
                raise
            active_committed = True
            if state["state"] == "managed":
                backup_slot = 0
        post = inspect_target(target)
    except BaseException:
        if target_created and not active_committed and path_exists_no_follow(target):
            remove_created_target(target, target_parent_snapshot)
        raise
    return {
        "ok": True,
        "operation": operation,
        "setup_id": chosen_setup,
        "profile_id": chosen_profile,
        "permission_mode": profile["permission_mode"],
        "description": setup["description"],
        "target": str(target),
        "trusted_workspace": str(trusted_workspace),
        "changed": changed,
        "backup_slot": backup_slot,
        "state": post["state"],
        "cleanup_pending": cleanup_pending or post.get("cleanup_pending", False),
    }


def plan_setup(
    target: Path,
    *,
    setup_id: Optional[str],
    profile_id: Optional[str],
    workspace: Optional[str],
) -> dict[str, Any]:
    state = inspect_target(target)
    chosen_setup, chosen_profile = choose_setup_and_profile(
        state, setup_id, profile_id, operation="plan"
    )
    existing_settings = read_existing_settings_if_managed(target, state)
    if workspace:
        trusted_workspace = canonical_workspace(workspace, default=target)
    elif state.get("state") == "managed":
        trusted_workspace = Path(str(state["trusted_workspace"]))
    else:
        trusted_workspace = target
    _setup, profile, desired = render_setup(
        chosen_setup,
        chosen_profile,
        canonical_target=target,
        trusted_workspace=trusted_workspace,
        existing_settings=existing_settings,
    )
    if state["state"] == "managed":
        changed = changed_paths(target, desired)
        backup_required = bool(changed)
        operation = "update" if state.get("setup_id") == chosen_setup else "switch"
    elif state["state"] in {"missing", "unmanaged"}:
        changed = sorted(relative.as_posix() for relative in desired)
        backup_required = False
        operation = "install"
    else:
        fail(f"unsupported target state: {state['state']}")
    return {
        "ok": True,
        "operation": operation,
        "setup_id": chosen_setup,
        "profile_id": chosen_profile,
        "permission_mode": profile["permission_mode"],
        "target": str(target),
        "trusted_workspace": str(trusted_workspace),
        "state": state["state"],
        "mutates": False,
        "backup_required": backup_required,
        "changed": changed,
        "cleanup_pending": state.get("cleanup_pending", False),
    }


def migrate_setup(
    target: Path, *, profile_id: Optional[str], workspace: Optional[str]
) -> dict[str, Any]:
    state = inspect_target(target)
    if state["state"] == "managed":
        return setup_lifecycle(
            target,
            setup_id=DEFAULT_SETUP_ID,
            profile_id=profile_id,
            workspace=workspace,
            operation="migrate",
        )
    if state["state"] == "unmanaged":
        fail("unmanaged target cannot be migrated automatically")
    return setup_lifecycle(
        target,
        setup_id=DEFAULT_SETUP_ID,
        profile_id=profile_id,
        workspace=workspace,
        operation="migrate",
    )


def remove_setup(target: Path) -> dict[str, Any]:
    state = inspect_target(target)
    if state["state"] != "managed":
        fail("target is not managed by nddev-qoder-cli-app")
    cleanup_pending = False
    try:
        cleanup_pending = remove_managed_state(target, backup_state=state)
    except CleanupFailure:
        cleanup_pending = True
    post = inspect_target(target)
    return {
        "ok": True,
        "operation": "remove",
        "target": str(target),
        "removed_setup_id": state["setup_id"],
        "removed_profile_id": state["profile_id"],
        "backup_slot": 0,
        "cleanup_pending": cleanup_pending or post.get("cleanup_pending", False),
    }


def restore_backup(target: Path, slot: int) -> dict[str, Any]:
    state = inspect_target(target)
    if state["state"] != "managed":
        fail("target is not managed by nddev-qoder-cli-app")
    envelope, desired = load_backup(target, slot)
    cleanup_pending = False
    try:
        cleanup_pending = apply_managed_state(target, desired)
    except CleanupFailure:
        cleanup_pending = True
    post = inspect_target(target)
    return {
        "ok": True,
        "operation": "restore",
        "target": str(target),
        "setup_id": post["setup_id"],
        "profile_id": post["profile_id"],
        "restored_from_slot": slot,
        "restored_source_setup_id": envelope["source_setup_id"],
        "cleanup_pending": cleanup_pending or post.get("cleanup_pending", False),
    }


def qoder_executable(target: Path) -> Path:
    return target / "bin" / COMMAND_NAME


def software_manifest_path(target: Path) -> Path:
    return target / "software" / SOFTWARE_MANIFEST_NAME


def software_presence(target: Path) -> dict[str, Any]:
    replace_paths_present = [
        relative.as_posix()
        for relative in SOFTWARE_REPLACE_PATHS
        if path_exists_no_follow(target / relative)
    ]
    owned_parent_paths_present = [
        relative.as_posix()
        for relative in SOFTWARE_PARENT_PATHS
        if path_exists_no_follow(target / relative)
    ]
    if not replace_paths_present:
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
    content, info = read_regular_file(path, label, max_bytes=max_bytes)
    return sha256_bytes(content), info.st_size


def expected_software_identity(asset_key: str, asset: dict[str, Any], root: Path) -> dict[str, Any]:
    size = asset.get("size")
    url = asset.get("url")
    digest = asset.get("sha256")
    filename = asset.get("filename")
    if not isinstance(size, int) or size <= 0:
        fail(f"baseline asset {asset_key} is missing exact size")
    if not all(isinstance(item, str) for item in (url, digest, filename)):
        fail(f"baseline asset {asset_key} is incomplete")
    executable_sha256, executable_size = file_sha256(qoder_executable(root), "Qoder CLI executable")
    return {
        "schema_version": 1,
        "product_name": PRODUCT_NAME,
        "version": TESTED_VERSION,
        "executable": f"bin/{COMMAND_NAME}",
        "asset": asset_key,
        "asset_filename": filename,
        "asset_size": size,
        "asset_sha256": digest,
        "source": url,
        "executable_sha256": executable_sha256,
        "executable_size": executable_size,
        "installer": {
            "source": "official-release-asset",
            "install_script_url": INSTALL_SCRIPT_URL,
            "install_script_sha256": INSTALL_SCRIPT_SHA256,
            "reason": "The official installer has no target prefix contract; this manager uses the verified release asset directly.",
        },
    }


def validate_existing_software_surface(target: Path) -> None:
    if path_exists_no_follow(target / "bin"):
        require_private_directory(target / "bin", "software bin directory")
    if path_exists_no_follow(target / "software"):
        require_private_directory(target / "software", "software metadata directory")
    if path_exists_no_follow(qoder_executable(target)):
        require_safe_executable(qoder_executable(target), "Qoder CLI executable")
    if path_exists_no_follow(software_manifest_path(target)):
        require_regular_file(software_manifest_path(target), "software manifest", owner_only=True)


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
            "cleanup_pending": False,
        }
    require_private_directory(target, "software target")
    pending, cleanup = cleanup_pending_state(target, read_only=True)
    presence = software_presence(target)
    executable = qoder_executable(target)
    manifest = software_manifest_path(target)
    if presence["software_state"] != "installed":
        return {
            "ok": True,
            "installed": False,
            "current": False,
            "target": str(target),
            "version": None,
            "executable": str(executable),
            **presence,
            **cleanup,
        }
    try:
        validate_existing_software_surface(target)
        info = load_json_object(manifest, "software manifest", owner_only=True)
        asset_key = str(info.get("asset", ""))
        baseline = load_baseline()
        asset = baseline.get("manifest", {}).get("files", {}).get(asset_key)
        if not isinstance(asset, dict):
            fail("software manifest references an unknown Qoder CLI asset")
        expected = expected_software_identity(asset_key, asset, target)
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
        "target": str(target),
        "version": info.get("version"),
        "executable": str(executable),
        **presence,
        **cleanup,
        **({"validation_error": validation_error} if validation_error else {}),
    }


def validate_current_software_postcondition(target: Path) -> None:
    presence = software_presence(target)
    if presence["software_state"] != "installed":
        fail("installed Qoder CLI failed validation: software files are incomplete")
    validate_existing_software_surface(target)
    info = load_json_object(software_manifest_path(target), "software manifest", owner_only=True)
    asset_key = str(info.get("asset", ""))
    baseline = load_baseline()
    asset = baseline.get("manifest", {}).get("files", {}).get(asset_key)
    if not isinstance(asset, dict):
        fail("installed Qoder CLI failed validation: unknown asset")
    expected = expected_software_identity(asset_key, asset, target)
    if info != expected:
        fail("installed Qoder CLI failed validation: manifest identity mismatch")


def download_asset_bytes(url: str, *, expected_size: int) -> bytes:
    if expected_size <= 0 or expected_size > SOFTWARE_DOWNLOAD_MAX_BYTES:
        fail(f"Qoder CLI expected asset size is outside the pinned product bound: {expected_size}")
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
            read_size = min(1024 * 1024, expected_size + 1 - total)
            if read_size <= 0:
                fail(f"Qoder CLI download exceeded the exact {expected_size}-byte asset bound")
            chunk = response.read(read_size)
            if not chunk:
                break
            total += len(chunk)
            if total > expected_size:
                fail(f"Qoder CLI download exceeded the exact {expected_size}-byte asset bound")
            chunks.append(chunk)
    if total != expected_size:
        fail(f"Qoder CLI download size mismatch: expected {expected_size}, got {total}")
    return b"".join(chunks)


def validate_archive_member_path(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    if (
        "\x00" in normalized
        or normalized.startswith("//")
        or re.fullmatch(r"(?i)[a-z]:.*", normalized)
    ):
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
    accepted: list[tarfile.TarInfo] = []
    for member in members:
        if member.name == "." and member.isdir():
            continue
        path = validate_archive_member_path(member.name)
        key = path.as_posix()
        if (
            key == "._qodercli"
            and member.isfile()
            and member.size <= APPLEDOUBLE_METADATA_MAX_BYTES
        ):
            continue
        if key in seen:
            fail(f"duplicate archive member path: {member.name}")
        seen.add(key)
        if member.issym() or member.islnk() or member.isdev():
            fail(f"unsafe archive member type: {member.name}")
        if member.isfile():
            total_size += member.size
            if total_size > SOFTWARE_TREE_MAX_BYTES:
                fail(f"Qoder CLI archive exceeds the {SOFTWARE_TREE_MAX_BYTES}-byte limit")
        accepted.append(member)
    return accepted


def safe_zip_infos(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > SOFTWARE_TREE_MAX_PATHS:
        fail(f"Qoder CLI archive exceeds the {SOFTWARE_TREE_MAX_PATHS}-path limit")
    seen: set[str] = set()
    total_size = 0
    for info in infos:
        path = validate_archive_member_path(info.filename)
        key = path.as_posix()
        if key in seen:
            fail(f"duplicate archive member path: {info.filename}")
        seen.add(key)
        mode = (info.external_attr >> 16) & 0o170000
        if info.is_dir():
            if mode and not stat.S_ISDIR(mode):
                fail(f"unsafe archive member type: {info.filename}")
            continue
        if mode and not stat.S_ISREG(mode):
            fail(f"unsafe archive member type: {info.filename}")
        total_size += info.file_size
        if total_size > SOFTWARE_TREE_MAX_BYTES:
            fail(f"Qoder CLI archive exceeds the {SOFTWARE_TREE_MAX_BYTES}-byte limit")
    return infos


def chmod_private_tree(root: Path) -> None:
    total_paths = 0
    total_bytes = 0
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts)):
        total_paths += 1
        if total_paths > SOFTWARE_TREE_MAX_PATHS:
            fail(f"Qoder CLI archive exceeds the {SOFTWARE_TREE_MAX_PATHS}-path limit")
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            fail(f"Qoder CLI archive contains symlink: {path.relative_to(root)}")
        if stat.S_ISDIR(info.st_mode):
            os.chmod(path, OWNER_DIRECTORY_MODE)
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            fail(f"Qoder CLI archive contains unsupported file: {path.relative_to(root)}")
        total_bytes += info.st_size
        if total_bytes > SOFTWARE_TREE_MAX_BYTES:
            fail(f"Qoder CLI archive exceeds the {SOFTWARE_TREE_MAX_BYTES}-byte limit")
        os.chmod(path, OWNER_EXECUTABLE_MODE if mode_of(info) & stat.S_IXUSR else OWNER_FILE_MODE)


def extract_archive(archive_path: Path, unpacked: Path) -> None:
    unpacked.mkdir(mode=OWNER_DIRECTORY_MODE)
    os.chmod(unpacked, OWNER_DIRECTORY_MODE)
    if archive_path.name.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as archive:
            for info in safe_zip_infos(archive):
                member_path = validate_archive_member_path(info.filename)
                destination = unpacked / Path(member_path.as_posix())
                if info.is_dir():
                    ensure_private_directory(destination)
                    continue
                ensure_private_parent(destination, f"archive member {info.filename}")
                with archive.open(info) as source, destination.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
                member_mode = (info.external_attr >> 16) & 0o777
                os.chmod(
                    destination,
                    OWNER_EXECUTABLE_MODE if member_mode & stat.S_IXUSR else OWNER_FILE_MODE,
                )
    else:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in safe_tar_members(archive):
                member_path = validate_archive_member_path(member.name)
                destination = unpacked / Path(member_path.as_posix())
                if member.isdir():
                    ensure_private_directory(destination)
                    continue
                source = archive.extractfile(member)
                if source is None:
                    fail(f"cannot extract archive member: {member.name}")
                ensure_private_parent(destination, f"archive member {member.name}")
                with source, destination.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
                os.chmod(
                    destination,
                    OWNER_EXECUTABLE_MODE if member.mode & stat.S_IXUSR else OWNER_FILE_MODE,
                )
    chmod_private_tree(unpacked)


def find_extracted_binary(unpacked: Path) -> Path:
    candidates: list[Path] = []
    for candidate in sorted(unpacked.rglob("*")):
        if candidate.name == COMMAND_NAME and candidate.is_file():
            require_safe_executable(candidate, "extracted Qoder CLI executable")
            candidates.append(candidate)
    if len(candidates) != 1:
        fail("Qoder CLI archive must contain exactly one qodercli executable")
    return candidates[0]


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
    home = stage / "home"
    runtime = stage / "runtime"
    tmp = runtime / "tmp"
    for directory in (
        home,
        runtime,
        tmp,
        runtime / "xdg-config",
        runtime / "xdg-cache",
        runtime / "xdg-state",
    ):
        directory.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
        os.chmod(directory, OWNER_DIRECTORY_MODE)
    return {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TMPDIR": str(tmp),
        "XDG_CONFIG_HOME": str(runtime / "xdg-config"),
        "XDG_CACHE_HOME": str(runtime / "xdg-cache"),
        "XDG_STATE_HOME": str(runtime / "xdg-state"),
        "QODER_CONFIG_DIR": str(stage / "qoder-config"),
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
    if sha256_bytes(archive_bytes) != expected_sha:
        fail(f"downloaded Qoder CLI digest mismatch for {asset_key}")
    archive_path = staging / Path(url).name
    write_durable_file(archive_path, archive_bytes)
    unpacked = staging / "unpacked"
    extract_archive(archive_path, unpacked)
    binary_source = find_extracted_binary(unpacked)
    live = staging / "live"
    ensure_private_directory(live / "bin")
    ensure_private_directory(live / "software")
    executable = live / "bin" / COMMAND_NAME
    shutil.copy2(binary_source, executable)
    os.chmod(executable, OWNER_EXECUTABLE_MODE)
    fsync_parent(executable)
    probe_staged_binary(executable, staging)
    manifest = expected_software_identity(asset_key, asset, live)
    write_durable_file(live / "software" / SOFTWARE_MANIFEST_NAME, canonical_json(manifest))
    return live


def apply_software_state(
    target: Path,
    live_stage: Optional[Path],
    *,
    remove: bool = False,
    extra_cleanup_sources: Optional[list[tuple[Path, str]]] = None,
) -> bool:
    txn = UndoTransaction(target, "software")
    try:
        validate_existing_software_surface(target)
        candidates: list[tuple[Path, str]] = []
        if remove:
            moved_parents: set[Path] = set()
            owned_children = {
                Path("bin"): {COMMAND_NAME},
                Path("software"): {SOFTWARE_MANIFEST_NAME},
            }
            for relative in SOFTWARE_PARENT_PATHS:
                parent = target / relative
                if path_exists_no_follow(parent) and parent.is_dir() and not parent.is_symlink():
                    children = {child.name for child in parent.iterdir()}
                    if children and children <= owned_children[relative]:
                        candidates.append((relative, "software-undo"))
                        moved_parents.add(relative)
            for relative in SOFTWARE_REPLACE_PATHS:
                if relative.parent in moved_parents:
                    continue
                if path_exists_no_follow(target / relative):
                    candidates.append((relative, "software-undo"))
        else:
            for relative in SOFTWARE_REPLACE_PATHS:
                if path_exists_no_follow(target / relative):
                    candidates.append((relative, "software-undo"))
        for relative, kind in txn.prepare_hold_moves(candidates):
            moved = txn.move_existing_to_hold(relative, kind=kind)
            if moved is not None:
                txn.mark_for_cleanup(relative, kind)
        if not remove:
            assert live_stage is not None
            for relative in SOFTWARE_REPLACE_PATHS:
                content, _ = read_regular_file(
                    live_stage / relative,
                    f"staged software {relative}",
                    max_bytes=SOFTWARE_TREE_MAX_BYTES,
                    allow_nlink=None,
                )
                txn.write_file(relative, content, executable=relative == Path("bin") / COMMAND_NAME)
        if remove:
            if software_presence(target)["software_state"] != "absent":
                fail("removed Qoder CLI software failed postcondition")
        else:
            validate_current_software_postcondition(target)
    except BaseException:
        txn.rollback()
        raise
    return finish_transaction(txn, extra_cleanup_sources)


def install_or_update_cli(target: Path, *, operation: str) -> dict[str, Any]:
    target_parent_snapshot = directory_snapshot(target.parent, "target parent")
    target_created = ensure_target_directory(target)
    active_committed = False
    staging: Optional[Path] = None
    try:
        preflight = software_status(target)
        if operation == "install-cli" and preflight["software_state"] != "absent":
            fail("Qoder CLI software already exists at target; use update-cli")
        if operation == "update-cli":
            if preflight["software_state"] == "absent":
                fail("Qoder CLI is not installed at the selected target; use install-cli")
            if preflight["installed"] and preflight["current"]:
                return {
                    "ok": True,
                    "operation": operation,
                    "target": str(target),
                    "changed": False,
                    "version": preflight["version"],
                    "executable": preflight["executable"],
                    "cleanup_pending": preflight.get("cleanup_pending", False),
                }
        asset_key, asset = select_platform_asset()
        staging = cleanup_parent(target) / f".stage-{uuid.uuid4().hex}"
        ensure_private_directory(staging)
        cleanup_pending = False
        live_stage = build_live_stage(staging, asset_key, asset)
        try:
            cleanup_pending = apply_software_state(
                target,
                live_stage,
                extra_cleanup_sources=[(staging, "stage-undo")],
            )
        except CleanupFailure:
            cleanup_pending = True
        except CleanupJournalPublishedInvalid:
            active_committed = True
            raise
        active_committed = True
        status = software_status(target)
    except BaseException:
        if target_created and not active_committed and path_exists_no_follow(target):
            remove_created_target(target, target_parent_snapshot)
        elif not active_committed and staging is not None and path_exists_no_follow(staging):
            authority_paths = (
                cleanup_prepare_intent_path(target),
                cleanup_intent_path(target),
                cleanup_journal_path(target),
                cleanup_retired_path(target),
            )
            if any(path_exists_no_follow(path) for path in authority_paths):
                fail("uncommitted software stage cannot be removed while cleanup authority exists")
            delete_bounded_uncommitted_tree(staging)
            if path_exists_no_follow(cleanup_parent(target)) and not any(
                cleanup_parent(target).iterdir()
            ):
                cleanup_parent(target).rmdir()
                fsync_parent(cleanup_parent(target))
        raise
    return {
        "ok": True,
        "operation": operation,
        "target": str(target),
        "changed": True,
        "version": TESTED_VERSION,
        "asset": asset_key,
        "executable": status["executable"],
        "cleanup_pending": cleanup_pending or status.get("cleanup_pending", False),
    }


def remove_cli(target: Path) -> dict[str, Any]:
    status = software_status(target)
    if status["software_state"] == "absent":
        return {
            "ok": True,
            "operation": "remove-cli",
            "target": str(target),
            "changed": False,
            "cleanup_pending": status.get("cleanup_pending", False),
        }
    cleanup_pending = False
    try:
        cleanup_pending = apply_software_state(target, None, remove=True)
    except CleanupFailure:
        cleanup_pending = True
    post = software_status(target)
    return {
        "ok": True,
        "operation": "remove-cli",
        "target": str(target),
        "changed": True,
        "cleanup_pending": cleanup_pending or post.get("cleanup_pending", False),
    }


def isolated_child_environment(target: Path) -> dict[str, str]:
    home = target / "home"
    runtime = target / "runtime"
    tmp = runtime / "tmp"
    xdg_config = runtime / "xdg-config"
    xdg_cache = runtime / "xdg-cache"
    xdg_state = runtime / "xdg-state"
    for directory in (home, runtime, tmp, xdg_config, xdg_cache, xdg_state):
        ensure_private_directory(directory)
    env: dict[str, str] = {}
    for name in INHERITED_ENV:
        value = os.environ.get(name)
        if value:
            env[name] = value
    token = os.environ.get("QODER_PERSONAL_ACCESS_TOKEN")
    if token:
        env["QODER_PERSONAL_ACCESS_TOKEN"] = token
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "TMPDIR": str(tmp),
            "XDG_CONFIG_HOME": str(xdg_config),
            "XDG_CACHE_HOME": str(xdg_cache),
            "XDG_STATE_HOME": str(xdg_state),
            "QODER_CONFIG_DIR": str(target),
        }
    )
    return env


def forbidden_launch_option(argument: str) -> Optional[str]:
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
            fail(f"launch argument is manager-owned and cannot be overridden: {option}")
    return forwarded


def verify_launch_trust(target: Path, workspace: Path, state: dict[str, Any]) -> None:
    settings = load_json_object(target / "settings.json", "settings.json", owner_only=True)
    permissions = settings.get("permissions")
    if not isinstance(permissions, dict):
        fail("settings.json permissions are malformed")
    trust = permissions.get("trustDirectories")
    if trust != [str(workspace)]:
        fail(
            "trusted workspace is not manager-owned in settings.json; run setup update "
            f"--workspace {workspace}"
        )
    if state["trusted_workspace"] != str(workspace):
        fail("setup stamp is not bound to the requested trusted workspace")
    if settings.get("general", {}).get("defaultPermissionMode") != state["permission_mode"]:
        fail("settings.json permission mode drift detected")


def launch_qoder(target: Path, args: list[str], *, workspace: Optional[str]) -> int:
    require_supported_product_host()
    forwarded = normalize_launch_arguments(args)
    lexical = require_lexical_absolute_target(str(target))
    with product_lifecycle_lock(shared=False, create=True):
        canonical = resolve_target_under_coordination(lexical, mutation=True)
        with target_lifecycle_lock(canonical, shared=False, create=True):
            state = inspect_target(canonical)
            if state["state"] != "managed":
                fail("target is not managed by nddev-qoder-cli-app")
            if state.get("cleanup_pending"):
                fail("pending post-commit cleanup must be drained before launch")
            status = software_status(canonical)
            if status.get("cleanup_pending"):
                fail("pending post-commit cleanup must be drained before launch")
            if not status["installed"] or not status["current"]:
                fail("Qoder CLI is not installed at the tested version in this target")
            trusted_workspace = canonical_workspace(workspace, default=Path.cwd())
            verify_launch_trust(canonical, trusted_workspace, state)
            command = [
                str(qoder_executable(canonical)),
                "--config-dir",
                str(canonical),
                "--cwd",
                str(trusted_workspace),
                "--plugin-dir",
                str(canonical / "plugins" / "nddev-builder"),
                *list(state["launch_args"]),
                *forwarded,
            ]
            env = isolated_child_environment(canonical)
            try:
                completed = subprocess.run(
                    command,
                    cwd=trusted_workspace,
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


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise JsonArgumentError(message)

    def exit(self, status: int = 0, message: Optional[str] = None) -> None:
        if status:
            raise JsonArgumentError((message or "").strip() or f"argument parser exited {status}")
        super().exit(status, message)


def add_json_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print JSON output")


def add_target_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", required=True, help="explicit absolute Qoder CLI target")


def add_setup_profile_workspace_arguments(
    parser: argparse.ArgumentParser, *, setup_default: bool = True
) -> None:
    parser.add_argument("--setup", default=DEFAULT_SETUP_ID if setup_default else None)
    parser.add_argument("--profile", default=DEFAULT_PROFILE_ID if setup_default else None)
    parser.add_argument("--workspace", help="absolute trusted workspace stored in Qoder settings")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = JsonArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list", help="list setup and profile variants")
    add_json_argument(list_parser)
    for name in ("status", "software-status"):
        command_parser = subparsers.add_parser(name, help=f"{name} for a target")
        add_target_argument(command_parser)
        add_json_argument(command_parser)
    plan_parser = subparsers.add_parser("plan", help="plan managed setup state")
    add_target_argument(plan_parser)
    add_setup_profile_workspace_arguments(plan_parser, setup_default=True)
    add_json_argument(plan_parser)
    install_parser = subparsers.add_parser("install", help="install managed setup state")
    add_target_argument(install_parser)
    add_setup_profile_workspace_arguments(install_parser, setup_default=True)
    add_json_argument(install_parser)
    update_parser = subparsers.add_parser("update", help="update the current managed setup state")
    add_target_argument(update_parser)
    add_setup_profile_workspace_arguments(update_parser, setup_default=False)
    add_json_argument(update_parser)
    switch_parser = subparsers.add_parser("switch", help="switch managed setup/profile state")
    add_target_argument(switch_parser)
    add_setup_profile_workspace_arguments(switch_parser, setup_default=True)
    add_json_argument(switch_parser)
    migrate_parser = subparsers.add_parser("migrate", help="migrate legacy managed Qoder state")
    add_target_argument(migrate_parser)
    migrate_parser.add_argument("--profile", default=DEFAULT_PROFILE_ID)
    migrate_parser.add_argument(
        "--workspace", help="absolute trusted workspace stored in Qoder settings"
    )
    add_json_argument(migrate_parser)
    restore_parser = subparsers.add_parser("restore", help="restore a target-bound backup")
    add_target_argument(restore_parser)
    restore_parser.add_argument("--backup", type=int, required=True)
    add_json_argument(restore_parser)
    remove_parser = subparsers.add_parser("remove", help="remove nddev-managed setup files")
    add_target_argument(remove_parser)
    add_json_argument(remove_parser)
    for name in ("install-cli", "update-cli", "remove-cli"):
        command_parser = subparsers.add_parser(name, help=f"{name} exact tested Qoder CLI")
        add_target_argument(command_parser)
        add_json_argument(command_parser)
    launch_parser = subparsers.add_parser("launch", help="launch target-owned Qoder CLI")
    add_target_argument(launch_parser)
    launch_parser.add_argument("--workspace", help="absolute trusted workspace for --cwd")
    add_json_argument(launch_parser)
    launch_parser.add_argument("qoder_args", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def error_result(message: str, *, json_output: bool) -> int:
    if json_output:
        print(json.dumps({"ok": False, "error": message}, indent=2, sort_keys=True))
    else:
        print(f"error: {message}", file=sys.stderr)
    return 2


def json_requested(argv: Optional[list[str]]) -> bool:
    values = argv if argv is not None else sys.argv[1:]
    return "--json" in values


def run(args: argparse.Namespace) -> int:
    if args.command == "list":
        print_payload(
            {"ok": True, "setups": list_setups(), "profiles": list_profiles()},
            json_output=args.json,
        )
        return 0
    if args.command == "status":
        result = coordinate_target(args.target, mutation=False, action=inspect_target)
        print_payload({"ok": True, **result}, json_output=args.json)
        return 0
    if args.command == "software-status":
        result = coordinate_target(args.target, mutation=False, action=software_status)
        print_payload(result, json_output=args.json)
        return 0
    if args.command == "plan":
        result = coordinate_target(
            args.target,
            mutation=False,
            action=lambda target: plan_setup(
                target,
                setup_id=args.setup,
                profile_id=args.profile,
                workspace=args.workspace,
            ),
        )
        print_payload(result, json_output=args.json)
        return 0
    if args.command in {"install", "update", "switch"}:
        result = coordinate_target(
            args.target,
            mutation=True,
            action=lambda target: setup_lifecycle(
                target,
                setup_id=args.setup,
                profile_id=args.profile,
                workspace=args.workspace,
                operation=args.command,
            ),
        )
        print_payload(result, json_output=args.json)
        return 0
    if args.command == "migrate":
        result = coordinate_target(
            args.target,
            mutation=True,
            action=lambda target: migrate_setup(
                target,
                profile_id=args.profile,
                workspace=args.workspace,
            ),
        )
        print_payload(result, json_output=args.json)
        return 0
    if args.command == "restore":
        result = coordinate_target(
            args.target,
            mutation=True,
            action=lambda target: restore_backup(target, args.backup),
        )
        print_payload(result, json_output=args.json)
        return 0
    if args.command == "remove":
        result = coordinate_target(args.target, mutation=True, action=remove_setup)
        print_payload(result, json_output=args.json)
        return 0
    if args.command == "install-cli":
        result = coordinate_target(
            args.target,
            mutation=True,
            action=lambda target: install_or_update_cli(target, operation="install-cli"),
        )
        print_payload(result, json_output=args.json)
        return 0
    if args.command == "update-cli":
        result = coordinate_target(
            args.target,
            mutation=True,
            action=lambda target: install_or_update_cli(target, operation="update-cli"),
        )
        print_payload(result, json_output=args.json)
        return 0
    if args.command == "remove-cli":
        result = coordinate_target(args.target, mutation=True, action=remove_cli)
        print_payload(result, json_output=args.json)
        return 0
    if args.command == "launch":
        qoder_args = list(args.qoder_args)
        if qoder_args and qoder_args[0] == "--":
            qoder_args = qoder_args[1:]
        return launch_qoder(Path(args.target), qoder_args, workspace=args.workspace)
    fail(f"unknown command: {args.command}")
    return 2


def main(argv: Optional[list[str]] = None) -> int:
    try:
        args = parse_args(argv)
        return run(args)
    except JsonArgumentError as exc:
        return error_result(str(exc), json_output=json_requested(argv))
    except QoderCliSetupError as exc:
        return error_result(str(exc), json_output=json_requested(argv))


if __name__ == "__main__":
    raise SystemExit(main())
