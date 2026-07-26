# Security Policy

## Supported surface

Security reporting covers the setup catalog, lifecycle CLI, public contracts,
documentation, native Qoder CLI builder projection, and GitHub workflows in
this repository. Only the latest numeric release is supported.

## Reporting a vulnerability

Report vulnerabilities privately through GitHub Security Advisories for
`NDDev-it-com/nddev-qoder-cli-app`. Do not publish credentials, tokens, private
configuration, or backup contents in an issue or pull request.

## Baseline controls

- The CLI never defaults to a live Qoder home; target operations require an
  explicit absolute `--target`.
- Managed files reject symlinks, special files, and hard-link aliases.
- Setup switching preserves unmanaged target files and co-owned settings keys.
- Backup envelopes and installed stamps are bound to the canonical target.
- Mutations use a sibling lock, bounded backup rotation, postcondition checks,
  and rollback on failure.
- The builder capability is projected as local native Qoder plugin, skill,
  agent, and hook files. Marketplace provisioning is not performed by this
  manager.
