# Repository instructions

## Development status: ON PAUSE

Product feature work, vendor updates, refactoring, releases and runtime operations
remain paused until the owner explicitly reactivates them. Source CI maintenance
is active: it may maintain source checks, workflow callers and their documentation.
Do not dispatch repository workflows or run the manager, installer, provider,
sign-in or model probes as part of that maintenance.

- Treat this clone as one independent Git mutation boundary.
- Preserve unrelated branches, worktrees, submodules, and dirty changes.
- Follow the repository's local documentation and source-owned contracts.
- Keep secrets, credentials, runtime state, caches, logs, and generated evidence
  out of version control.

## Verification

- Source checks: `just check`.
- Full source and retained-history check: `just verify`.
