# Changelog

All notable changes to `nddev-qoder-cli-app` are documented here.

## [0.2.4] - 2026-08-08

- Update the official Qoder CLI baseline from `1.1.16` to stable `1.1.17`.
- Refresh the canonical channel manifest identity and all seven supported
  non-Windows archive sizes and SHA-256 digests; the installer bytes and
  target-prefix limitation remain unchanged.

## [0.2.3] - 2026-08-06

- Update the official Qoder CLI baseline from `1.1.15` to stable `1.1.16`.
- Refresh the canonical channel manifest identity and all seven supported
  non-Windows archive sizes and SHA-256 digests without broadening host support.

## [0.2.2] - 2026-08-05

- Update the official Qoder CLI baseline from `1.1.14` to stable `1.1.15`.
- Refresh the canonical channel manifest identity and all seven non-Windows
  archive sizes and SHA-256 digests while preserving the supported host model.

## [0.2.1] - 2026-08-05

- Update the official Qoder CLI baseline from `1.1.10` to stable `1.1.14`.
- Refresh the canonical channel-manifest identity plus exact digests and sizes
  for every macOS and Linux archive while preserving the existing host policy.
- Make lifecycle-anchor identity stable across manager releases, write the
  version-independent v2 binding, and retain strict read compatibility for
  exact legacy v1 anchors.

## [0.2.0] - 2026-07-28

- Updated the official Qoder CLI baseline to `1.1.10` with exact manifest,
  installer, release-page, and runtime archive observations.
- Replaced permission-mode setup variants with one `nddev-builder` content
  setup plus separate `full-auto` and `safe` launch profiles.
- Added setup `update`, `migrate`, `remove-cli`, target-owned software removal,
  QODER_CONFIG_DIR launch isolation, managed trust-directory verification, and
  caller credential allowlisting for `QODER_PERSONAL_ACCESS_TOKEN` only.
- Added macOS and Ubuntu-glibc product host gating while preserving upstream
  artifact names in a separate observation layer.
- Added monotonic lifecycle anchors, strict target-bound backups, immutable
  cleanup-pending state reporting, and public cache-free contract validation.

## [0.1.0] - 2026-07-26

- Initial target-explicit Qoder CLI setup manager.
- `safe`, `balanced`, and `full-auto` setup variants for the current permission
  mode, settings, and launch-flag surfaces.
- Default-on local native `nddev-builder` plugin projection with skills, agents,
  hooks, and user-file projections.
- Public contract validator and shared public CI callers.
