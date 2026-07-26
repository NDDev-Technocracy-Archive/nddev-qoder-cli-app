---
name: nddev-builder
description: Build and review native Qoder CLI configuration, plugin, skill, agent, hook, MCP, and setup artifacts for NDDev modules.
---

# NDDev Builder for Qoder CLI

Use current Qoder CLI surfaces only:

- user settings in `settings.json`;
- user instructions in `AGENTS.md`;
- skills under `skills/`;
- agents under `agents/`;
- hooks under `hooks/`;
- local plugins with `.qoder-plugin/plugin.json`;
- MCP configuration only when explicitly declared.

Do not assume a marketplace install flow unless a current official Qoder source
proves it. For this module, the marketplace field is null and the builder is
projected as local native files.

Keep auth and provider credentials outside generated artifacts.
