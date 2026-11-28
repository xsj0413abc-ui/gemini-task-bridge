# Changelog

## 2.2.2

Fixes from a long multi-pair session where Gemini.app froze twice and most tasks failed.

- **Gemini no longer freezes when several pairs are busy.** macOS hang reports showed Gemini 1.147's window stuck in a loop when the bridge switched chats and typed while another chat was still generating. Tasks now run one at a time across all pairs (`max_parallel_tasks`, default 1). Tasks also wait while a paired chat still shows "In progress". After switching chats the bridge waits for the window to settle, and it sets the prompt text once. `gemini_pair_init` stays pending while a task is running and creates the chat once Gemini is idle.
- **A frozen Gemini is reported.** `gemini_health`, `doctor` and `gemini_wait` say `gemini_not_responding` with the fix (force quit and reopen Gemini) instead of looking healthy, and dispatch pauses until Gemini answers again.
- **Stopped answers are no longer delivered as replies.** A chat that only shows "You stopped this response" fails with `gemini_response_stopped`. Partial answers are delivered with `captureChannel: ax_capture_stopped`. UI labels such as `info` are stripped.
- **Per-task time limit.** `gemini_send` takes `max_task_minutes` (CLI `--max-task-minutes`). On timeout the result includes `partialMessage` with what Gemini had written.
- Checked against Gemini.app 1.150.3.

## 2.2.1

- One-command install: `scripts/install.sh --install-service --add-to-path` installs the background service, adds `~/.local/bin` to `~/.zshrc` once (with a backup), and opens System Settings → Accessibility with `Python.app` selected in Finder when permission is missing (`--no-open` skips this).
- `SKILL.md` setup steps are written for an AI agent to run end to end, including MCP registration for every Claude config directory, Codex, and Antigravity.
- No runtime changes. Existing 2.2.0 installs keep working; re-run `install.sh` only if you want the new installer.

## 2.2.0

- Agents can create and pair Gemini Task conversations through the background service.
- Renamed the project to `gemini-task-bridge`. Existing commands, runtime paths, environment variables, and LaunchAgents remain compatible. No data is moved.
- CLI and MCP errors explain what went wrong and what to do next. `doctor` reports readiness in stages.

Upgrading from an older release? Rerun `scripts/install.sh`, then follow the controlled cutover in [SKILL.md](skills/gemini-task-bridge/SKILL.md) before enabling the new features.
