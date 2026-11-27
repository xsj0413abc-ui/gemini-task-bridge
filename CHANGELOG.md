# Changelog

## 2.2.0

- Agents can create and pair Gemini Task conversations through the background service.
- Renamed the project to `gemini-task-bridge`. Existing commands, runtime paths, environment variables, and LaunchAgents remain compatible. No data is moved.
- CLI and MCP errors explain what went wrong and what to do next. `doctor` reports readiness in stages.

Upgrading from an older release? Rerun `scripts/install.sh`, then follow the controlled cutover in [SKILL.md](skills/gemini-task-bridge/SKILL.md) before enabling the new features.
