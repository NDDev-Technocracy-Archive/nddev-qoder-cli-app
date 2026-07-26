# nddev-qoder-cli-app

Target-explicit NDDev setup manager for the current Qoder CLI.

This module manages an isolated Qoder home selected by `--target`; it never
defaults to the caller's live home directory and does not read or modify live
authentication state.

## Supported runtime baseline

- Command: `qodercli`
- Package identity: `@qoder-ai/qodercli`
- Tested version: `1.1.5`
- Published: `2026-07-24T11:00:12.692Z`
- Install manifest: `https://qoder-ide.oss-accelerate.aliyuncs.com/qodercli/channels/manifest.json`
- Config root: `QODER_HOME`

The full immutable asset baseline is in `references/qoder-cli-baseline.json`.

## Usage

```bash
python3 cli-tools/nddev_qoder_cli.py list
python3 cli-tools/nddev_qoder_cli.py plan --setup safe --target /absolute/qoder-home
python3 cli-tools/nddev_qoder_cli.py install --setup safe --target /absolute/qoder-home
python3 cli-tools/nddev_qoder_cli.py switch --setup balanced --target /absolute/qoder-home
python3 cli-tools/nddev_qoder_cli.py restore --backup 0 --target /absolute/qoder-home
python3 cli-tools/nddev_qoder_cli.py remove --target /absolute/qoder-home
python3 cli-tools/nddev_qoder_cli.py software-status --target /absolute/qoder-home
python3 cli-tools/nddev_qoder_cli.py install-cli --target /absolute/qoder-home
python3 cli-tools/nddev_qoder_cli.py launch --target /absolute/qoder-home -- --help
```

`install-cli` downloads the current tested Qoder release asset for the local
platform, checks its SHA-256 digest against the baseline, and installs only into
`<target>/bin/qodercli`.

## Setup variants

- `safe`: current default permission mode.
- `balanced`: `accept_edits` with bounded read/write/git allowlist and
  destructive command denials.
- `full-auto`: trusted-target setup using `bypass_permissions`.

All variants keep `nddev-builder` default-on through local native plugin, skill,
agent, hook, and user-file projections. Marketplace provisioning is `null`
because this manager does not have a confirmed official marketplace install
contract.

## Safety model

- explicit absolute `--target` is required for every target operation;
- target symlinks and managed symlinks/hard links fail closed;
- reads are size bounded;
- state stamps and backup envelopes bind to the canonical target;
- ten rotating target-bound backups are retained;
- unmanaged target files and co-owned settings keys are preserved;
- mutation failure rolls back the previous managed state;
- launcher child environment strips common Qoder token variables.
