---
name: gemini-task-bridge
description: Unofficial macOS bridge connecting Claude Code, Codex, Antigravity, and T3 Code to Google Gemini Desktop (Gemini.app) Task conversations via a zero-dependency stdio MCP server, background control plane, and CLI. Use when installing, diagnosing (doctor), or using gemini-task-bridge (or legacy t3-gemini-bridge), creating or binding Gemini Task chats with gemini_pair_init or gemini_list_chats, dispatching tasks via MCP (gemini_send, gemini_wait, gemini_read) or CLI (send, task wait), handling needs_attention cards, or switching routing modes. 适用于在 macOS 上通过 MCP 或 CLI 桥接 Gemini Desktop Task 对话、自动新建对话、派发任务与排查报错。Do not use for Gemini CLI, API keys in provider settings, or non-macOS hosts.
license: MIT
metadata:
  short-description: Unofficial macOS bridge to Gemini Desktop Task mode for Claude Code, Codex, Antigravity, and T3 Code
  platform: macos
  version: "2.2.1"
---

# Gemini Task Bridge (`gemini-task-bridge`)

Operating manual for bridging **Claude Code**, **Codex**, **Antigravity**, and **T3 Code** with **Google Gemini Desktop (`Gemini.app`) Task mode** on **macOS** (unofficial tool, zero third-party dependencies).

**Hard boundaries:**
- macOS only. Never edit T3 Code's SQLite database (`statev2.sqlite`), copy provider session tokens, or touch Gemini Desktop's SwiftData store.
- Only the background LaunchAgent (`Python.app`) needs Accessibility permission. Host processes submit requests via the atomic control queue and never run Accessibility UI actions directly.

---

## Step 0: Decision Flow (Zero to First Completed Task)

Run this decision tree first so you can guide any user from not installed to a completed task:

1. **Installed?**
   - Call `gemini_health` if MCP is connected; otherwise check `command -v gemini-task-bridge` or `~/.local/bin/gemini-task-bridge` (compat alias: `t3-gemini-bridge`).
   - **Not installed** → Walk the user through **Workflow 1**. Run the commands yourself; the user only needs to toggle Accessibility in System Settings.
2. **Ready? (`doctor`)**
   - Run `gemini-task-bridge doctor --json` (or read `gemini_health` if MCP-only).
   - Judge readiness strictly from `ready` / `daemonHealth`, never `currentProcess.axTrusted` (reference only under `advanced`).
   - **If `ready != true`**: Read `stage` (`daemon` → `daemon_accessibility` → `screen` → `gemini`) and `next`, and give the user the exact blocker + command/Settings path.
3. **Usable pair exists?**
   - Call `gemini_pairs` (MCP) or `gemini-task-bridge pair list --json` (CLI).
   - **No usable pair** → Immediately call **MCP `gemini_pair_init`** (preferred) or CLI `pair init` to create a dedicated Gemini Task chat, then proceed directly to dispatch.
   - **Pair exists** → Go straight to **Workflow 3**.

---

## Workflow 1 — Install and Verify the Bridge

Run these steps for the user.

1. From the skill directory, install the CLI and LaunchAgent and set up PATH:
   ```bash
   bash scripts/install.sh --install-service --add-to-path
   ```
2. In **System Settings → Privacy & Security → Accessibility**, ask the user to enable the `Python.app` selected in Finder (add it if it is not listed). If the installer didn't open Settings and Accessibility is needed, use its printed TCC Python path to locate the app. Poll `"$HOME/.local/bin/gemini-task-bridge" doctor --json` every few seconds until `ready: true`. If blocked, follow `next` (for example, restart the service if the toggle is on but the daemon still lacks trust). For LaunchAgent conflicts or upgrades, see [macOS troubleshooting](references/macos-permissions-and-troubleshooting.md).
3. Register MCP for the user's host with the absolute wrapper path:
   - **Claude Code** — run `env -u CLAUDE_CONFIG_DIR claude mcp add --transport stdio --scope user gemini-task-bridge -- "$HOME/.local/bin/gemini-task-bridge" mcp --scope claude-code` for the default config. Repeat with `CLAUDE_CONFIG_DIR` set to each custom config directory the user uses.
   - **Codex** — run `codex mcp add gemini-task-bridge -- "$HOME/.local/bin/gemini-task-bridge" mcp --scope codex`.
   - **Antigravity** — back up `~/.gemini/config/mcp_config.json` if it exists, then parse it (treat a missing or empty file as `{}`). Merge only the `gemini-task-bridge` entry into `mcpServers`, with `command` set to the absolute wrapper path and `args` set to `["mcp", "--scope", "antigravity"]`. Preserve all other entries and keys.
4. Ask the user to start a new host session. In it, call `gemini_pair_init` with a unique test title, use `gemini_send` to ask Gemini to echo a random code, then use `gemini_wait` to verify the reply contains that exact code.

---

## Workflow 2 — Create or Bind a 1-to-1 Pair

Enabled pairs enforce global 1-to-1 `geminiTitle` uniqueness (`local` + `t3`).

### Preferred: MCP `gemini_pair_init` (Daemon Control Plane)

Creates a `Gemini.app` Task conversation via the background daemon—no host Accessibility needed:
- Params: `title` *(required)*, `pair_id` *(auto `<scope>-<slug-or-6hex>`)*, `label`, `workspace` *(abs path)*, `idempotency_key` *(default `sha256(scope|title)[:32]`)*, `timeout_seconds` *(≤ 300, default `150`)*.
- Always creates `adapter=local` with `ownerScope` = MCP `--scope` (or `"default"`).
- **Quota:** Max **10 enabled pairs per scope** (`pair_limit_reached`).
- **Timeout:** Returns `completed: false` + `requestId`. Re-call with the exact same arguments to resume checking.
- Call `gemini_list_chats` to list sidebar titles (bound status & scope visibility; never exposes other scopes' `pairId`).

### Secondary: CLI `pair init` / `pair request`

When the daemon is fresh, has `daemon_control_plane`, and `axTrusted == true`, `pair init` and `pair gemini-chats` automatically use the background control plane:

```bash
# Local pair
gemini-task-bridge pair init --id "reviewer" --adapter local \
  --gemini-title "Code Reviewer" --workspace "/abs/path/to/workspace"

# T3 Code bidirectional pair (omit --reply-to to reply to --t3-thread)
gemini-task-bridge pair init --id "t3_reviewer" --adapter t3 \
  --t3-thread "<target-t3-thread-id>" --reply-to "<orchestrator-t3-thread-id>" \
  --gemini-title "T3 Code Reviewer" --workspace "/abs/path/to/workspace"

# Poll a pending/async control request (exit 3 = still pending)
gemini-task-bridge pair request "<requestId>" --wait 60 --json
```

On `workspace_not_bookmarked_in_gemini`, ask the user to select that folder once via **Folders → Add new folder...** in `Gemini.app`, then retry.

---

## Workflow 3 — Dispatch, Wait & Read Replies

- **MCP (preferred):** `gemini_health`, `gemini_pairs`, `gemini_list_chats`, `gemini_pair_init`, `gemini_send` *(non-blocking by default → `taskId`)*, `gemini_wait` *(chunked `1..300s`)*, `gemini_read`. Accepts snake_case and camelCase aliases (`pairId`, `taskId`, `idempotencyKey`).
- **CLI:**
  ```bash
  gemini-task-bridge send --pair "<pair-id>" --idempotency-key "req-001" --message "Review /abs/path/to/spec.md"
  gemini-task-bridge task wait "<taskId>" --timeout 120 --return-on-attention --json
  gemini-task-bridge task read "<taskId>" --json
  ```
- **Rules:** Never hand-write `[[T3_BRIDGE_START:` / `[[T3_BRIDGE_DONE:` sentinels or `outbox/*.json` instructions; always pass absolute paths; custom `--task-id` cannot end with `.receipt`; same `pairId` runs FIFO, distinct `pairId`s run concurrently.

---

## Workflow 4 — Routing & Emergency Rollback

```bash
gemini-task-bridge mode t3-all            # t3 -> T3; new local sends fail closed (exit 2)
gemini-task-bridge mode hybrid            # Restore per-pair routing
gemini-task-bridge pair route "<t3-pair>" t3|gemini
```

---

## Workflow 5 — Handling Exceptions Safely

Every v2.2 CLI and MCP error returns `error`, `message`, and `fix` (localized via `GEMINI_TASK_BRIDGE_LANG=zh|en` or locale). **Relay `message` and `fix` directly to the user.**

| Error Code / Status | Meaning | Required Action |
| --- | --- | --- |
| `needs_attention` (exit `3`) | `Gemini.app` shows a confirmation card | Ask user to confirm in `Gemini.app`, then resume `gemini_wait`/`task wait` with the same `taskId`. **Do not resend.** |
| `ax_not_trusted` | Daemon lacks Accessibility trust | Enable resolved `Python.app` in System Settings → Accessibility, then `service restart` + `doctor`. |
| `gemini_not_running` | `Gemini.app` is closed | Ask user to open `Gemini.app` and re-check `doctor`. |
| `screen_locked` | Mac display is locked | Wait for unlock; queues resume automatically. |
| `control_plane_unavailable` | Daemon stopped/stale or pre-v2.2 | Run `doctor` and follow `next`. |
| `control_request_timeout` (exit `3`) | Control op still running | Re-call `gemini_pair_init` with same params or run `pair request <requestId> --wait 60 --json`. |
| `control_request_unconfirmed` | Daemon crashed mid-create | Check `Gemini.app` sidebar; bind with `pair add` if present, else retry with a new key. |
| `pair_limit_reached` | Scope reached 10 enabled pairs | Reuse an existing pair or remove one (`pair remove <id>`). |
| `invalid_mcp_scope` | Invalid `--scope` format | Fix `--scope` (`^[a-z0-9][a-z0-9_-]{0,31}$`) in host MCP config and start a new session. |
| `conflicting_launchagent_for_root` | Another LaunchAgent uses this root | `launchctl bootout` + remove old plist, or set `GEMINI_TASK_BRIDGE_LABEL=<existing>`. |
| `workspace_not_bookmarked_in_gemini` | Folder never opened in `Gemini.app` | Add folder once in `Gemini.app` Folders menu, then retry. |
| `duplicate_gemini_title_in_pairs` | Title already bound | Pick a unique title or reuse the bound pair. |
| `pair_has_active_task` | Pair has an active task | Wait for task completion before editing/removing the pair. |
| `unknown_pair_id` | Missing or out-of-scope pair | Check `gemini_pairs` or `pair list --json`. |
| `daemon_upgrade_required_for_v21_features` | Active `v2.0` daemon on root | Follow Cutover SOP in [references/macos-permissions-and-troubleshooting.md](references/macos-permissions-and-troubleshooting.md). |
| `unknown_timeout` / `failed` | Uncertain submit or delivery timeout | **Never blindly resend with a new `taskId`.** Check `task status`, `sent/`, and the chat first. |

---

## Supporting References

- **CLI flags, exit codes, schemas, rename compatibility**: [references/cli-reference.md](references/cli-reference.md)
- **MCP tools, `--scope`, host configs, `--allow-pairs`, acceptance**: [references/mcp-and-multi-agent.md](references/mcp-and-multi-agent.md)
- **macOS `Python.app` TCC, `doctor` stages, Label conflicts, Cutover SOP**: [references/macos-permissions-and-troubleshooting.md](references/macos-permissions-and-troubleshooting.md)
- **Control queue, crash recovery, 3-phase intake, security invariants**: [references/architecture-and-safety.md](references/architecture-and-safety.md)
