# nddev-qoder-cli-app

Target-explicit NDDev setup manager for Qoder CLI.

This module manages one isolated Qoder target selected by `--target`. It never
defaults to the caller's live Qoder configuration, does not mutate live trust
state, and does not inherit caller home-directory credentials.

## Supported runtime baseline

- Command: `qodercli`
- Package identity: `@qoder-ai/qodercli`
- Tested version: `1.1.7`
- Published: `2026-07-28T10:58:43.023Z`
- Install manifest: `https://qoder-ide.oss-accelerate.aliyuncs.com/qodercli/channels/manifest.json`
- Managed config root: `QODER_CONFIG_DIR=<absolute-target>`

The immutable official observation, release-page split, archive names, sizes,
and SHA-256 digests are in `references/qoder-cli-baseline.json`.

## Usage

```bash
python3 cli-tools/nddev_qoder_cli.py list
python3 cli-tools/nddev_qoder_cli.py plan --target /absolute/qoder-target --workspace /absolute/workspace
python3 cli-tools/nddev_qoder_cli.py install --target /absolute/qoder-target --workspace /absolute/workspace
python3 cli-tools/nddev_qoder_cli.py update --target /absolute/qoder-target
python3 cli-tools/nddev_qoder_cli.py switch --target /absolute/qoder-target --profile safe
python3 cli-tools/nddev_qoder_cli.py restore --backup 0 --target /absolute/qoder-target
python3 cli-tools/nddev_qoder_cli.py remove --target /absolute/qoder-target
python3 cli-tools/nddev_qoder_cli.py software-status --target /absolute/qoder-target
python3 cli-tools/nddev_qoder_cli.py install-cli --target /absolute/qoder-target
python3 cli-tools/nddev_qoder_cli.py update-cli --target /absolute/qoder-target
python3 cli-tools/nddev_qoder_cli.py remove-cli --target /absolute/qoder-target
python3 cli-tools/nddev_qoder_cli.py launch --target /absolute/qoder-target --workspace /absolute/workspace -- --help
```

`install-cli` uses the official manifest archive for the current supported
product host. The official `https://qoder.com/install` script is pinned as
evidence, but production install uses the verified release asset directly
because the installer has no target-prefix contract.

## Setup And Profiles

Content setup is exactly `nddev-builder`. Launch permission behavior is selected
separately:

- `full-auto`: canonical `--permission-mode bypass_permissions`.
- `safe`: canonical `--permission-mode dont_ask`.

The manager owns the launch permission argument. Caller attempts to pass
`--permission-mode`, `--dangerously-skip-permissions`, `--yolo`, `--config-dir`,
`--cwd`, `--plugin-dir`, or related config/tool override flags fail closed.
Upstream safety checks or administrator policy may still constrain
`bypass_permissions`.

## Launch Environment

Launch uses isolated target-owned runtime directories:

- `HOME=<target>/home`
- `USERPROFILE=<target>/home`
- `TMPDIR=<target>/runtime/tmp`
- `XDG_CONFIG_HOME=<target>/runtime/xdg-config`
- `XDG_CACHE_HOME=<target>/runtime/xdg-cache`
- `XDG_STATE_HOME=<target>/runtime/xdg-state`
- `QODER_CONFIG_DIR=<target>`

Only locale/terminal variables and `QODER_PERSONAL_ACCESS_TOKEN` are inherited
from the caller. External Git, package-manager, and provider credentials from
the caller's home are intentionally not inherited.

Qoder CLI 1.1.7 does not provide a proven non-interactive trust command. This
manager writes and verifies `permissions.trustDirectories` in the target
`settings.json`; launch fails closed if the requested workspace is not exactly
the managed trusted workspace.

## Product Hosts

Production selection supports these NDDev host IDs:

- `macos-arm64`
- `macos-x64`
- `ubuntu-glibc-arm64`
- `ubuntu-glibc-x64`

Unsupported categories are `windows`, `non-ubuntu-linux`, `linux-musl`, and
`unsupported-architecture`. No official Ubuntu/glibc version floor is published,
so the Ubuntu floor is recorded as `null`.

## Safety Model

- explicit absolute `--target` is required for every target operation;
- supported-host preflight runs before target filesystem observation;
- read-only commands do not create product or target lock anchors;
- lifecycle anchors use a platform-stable same-UID protected root independent
  of caller `TMPDIR`;
- target symlinks and managed symlinks/hard links fail closed;
- archive extraction rejects traversal, symlinks, hard links, device nodes,
  unsafe modes, and oversized trees;
- state stamps and backups bind to the canonical target;
- target-owned backups bind exact path, size, and SHA-256 records and reject
  missing, extra, or tampered payloads;
- mutations use monotonic external coordination, durable prepare authority
  before active sources move into undo roots, rollback-preserving undo roots,
  and an immutable cleanup journal for post-commit recursive deletion;
- `launch` keeps target lifecycle coordination through the launched process
  lifetime;
- valid post-commit cleanup failures return success with top-level
  `cleanup_pending: true`; later mutations drain pending cleanup before active
  changes, while read-only commands only report or fail closed.

Public contract checks:

```bash
python3 cli-tools/validate_public_contracts.py
```
