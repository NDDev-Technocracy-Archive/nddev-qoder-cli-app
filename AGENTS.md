# Repository instructions

## Development status: ON PAUSE

Do not begin feature work, vendor updates, refactoring, releases, issue/PR
implementation, or repository workflow dispatches here. Resume module-specific
work only when the owner explicitly names this repository and directs its
reactivation. Read-only integrity inspection remains allowed.

- Treat this clone as one independent Git mutation boundary.
- Preserve unrelated branches, worktrees, submodules, and dirty changes.
- Follow the repository's local documentation and source-owned contracts.
- Keep secrets, credentials, runtime state, caches, logs, and generated evidence
  out of version control.

## Verification

- Test: `python3 -m json.tool config/nddev-contract.json`.
