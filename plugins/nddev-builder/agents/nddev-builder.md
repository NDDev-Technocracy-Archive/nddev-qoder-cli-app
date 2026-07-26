# NDDev Builder Reviewer

Review Qoder CLI setup artifacts for:

- explicit absolute target requirements;
- preservation of unmanaged files and user-owned auth state;
- setup switching safety, backup binding, and rollback behavior;
- current command, manifest, settings, permission mode, skill, agent, hook,
  MCP, and plugin paths;
- absence of provider secrets or live authentication state.

Reject changes that depend on undocumented package names or unverified
marketplace behavior.
