# Changelog

## 2.2.1

- One-command install: `scripts/install.sh --install-service --add-to-path` installs the background service, adds `~/.local/bin` to `~/.zshrc` once (with a backup), and opens System Settings → Accessibility with `Python.app` selected in Finder when permission is missing (`--no-open` skips this).
- `SKILL.md` setup steps are written for an AI agent to run end to end, including MCP registration for every Claude config directory, Codex, and Antigravity.
- No runtime changes. Existing 2.2.0 installs keep working; re-run `install.sh` only if you want the new installer.

## 2.2.0

- Agents can create and pair Gemini Task conversations through the background service.
- Renamed the project to `gemini-task-bridge`. Existing commands, runtime paths, environment variables, and LaunchAgents remain compatible. No data is moved.
- CLI and MCP errors explain what went wrong and what to do next. `doctor` reports readiness in stages.

Upgrading from an older release? Rerun `scripts/install.sh`, then follow the controlled cutover in [SKILL.md](skills/gemini-task-bridge/SKILL.md) before enabling the new features.
