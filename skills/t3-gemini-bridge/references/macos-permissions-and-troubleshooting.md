# macOS Permissions, Doctor Stages, Cutover & Troubleshooting

## 1. Why LaunchAgent Binds `Python.app` Instead of `/usr/bin/python3`

On macOS, `/usr/bin/python3` is an Apple developer tool shim that shares the exact same filesystem inode (typically ~78 hardlinks) with `/usr/bin/git`, `/usr/bin/clang`, and other Xcode Command Line Tools binaries.

When a `launchd` LaunchAgent executes `/usr/bin/python3` in the background, macOS `tccd` (Transparency, Consent, and Control) resolves the responsible binary via inode/code-identity heuristics and frequently misattributes the process as `/usr/bin/git`. This breaks `AXIsProcessTrusted()` silently after reboots or agent restarts even when `/usr/bin/python3` appears checked in System Settings.

`resolve_launchagent_python()` prevents this by probing for a dedicated single-hardlink (`st_nlink == 1`) `Python.app` binary in this order:

1. `<sys.base_prefix>/Resources/Python.app/Contents/MacOS/Python`
2. `/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/*/Resources/Python.app/Contents/MacOS/Python` (preferring the active Python minor version, then newest installed version)
3. `/Library/Frameworks/Python.framework/Versions/*/Resources/Python.app/Contents/MacOS/Python`, `/opt/homebrew/Frameworks/...`, and `/usr/local/Frameworks/...`
4. `os.path.realpath(sys.executable)` if `st_nlink == 1`

### Required macOS Permissions (v2.2.0)

1. **Accessibility** (**System Settings → Privacy & Security → Accessibility**):
   - Add and enable **only** the exact `Python.app` reported by `install.sh` (`TCC Python`) or `gemini-task-bridge service install` (`pythonExecutable`).
   - **AI hosts (Claude Code, Codex, Antigravity, T3 Code) and your terminal do NOT need Accessibility permission** in v2.2.0—`gemini_pair_init`, `gemini_list_chats`, `pair init`, and `pair gemini-chats` delegate UI actions to the background LaunchAgent via the control queue. (Terminal Accessibility is only needed if you explicitly pass `--direct` or run without the daemon.)
2. **Full Disk Access** (**System Settings → Privacy & Security → Full Disk Access**):
   - Required if paired Gemini workspaces or task files live under TCC-protected folders (`~/Desktop`, `~/Documents`, `~/Downloads`).
3. Restart the daemon (`gemini-task-bridge service restart`) after toggling TCC permissions so the process picks up the updated trust token.

---

## 2. Stage-by-Stage `doctor` Diagnosis & Stale Skill Detection

Run `gemini-task-bridge doctor` (human-readable `✅`/`❌` + next step) or `gemini-task-bridge doctor --json` (machine-readable).

`doctor` determines `ready` **strictly from `daemonHealth`** (never `currentProcess`, which is placed under `advanced` for reference only) and stops at the first failing `stage`:

| `stage` | Meaning | Typical Fix |
| --- | --- | --- |
| `daemon` | LaunchAgent is not installed, not running, or heartbeat is stale | Run `gemini-task-bridge service install` (or `service restart`). |
| `daemon_accessibility` | Running LaunchAgent reports `daemonHealth.axTrusted == false` | Enable the resolved `Python.app` in **System Settings → Privacy & Security → Accessibility**, then run `gemini-task-bridge service restart`. |
| `screen` | Mac display is locked (`daemonHealth.screenLocked == true`) | Unlock macOS; queued tasks and control requests resume automatically. |
| `gemini` | `Gemini.app` is not running (`daemonHealth.geminiRunning == false`) | Launch `/Applications/Gemini.app`, then re-run `gemini-task-bridge doctor`. |
| `ok` | All checks passed (`ready: true`) | Ready to create pairs (`gemini_pair_init`) and dispatch tasks (`gemini_send`). |

---

## 3. LaunchAgent Label Conflict Handling (`conflicting_launchagent_for_root`)

Before writing a plist, `gemini-task-bridge service install` scans all LaunchAgents in `~/Library/LaunchAgents/` targeting the same resolved data root (`_find_launchagents_for_root`).

If another plist with a **different Label** already points to that root (for example, a custom or legacy `t3-gemini-bridge` label), `service install` refuses to create a second daemon and fails with `conflicting_launchagent_for_root`, listing the conflicting label(s). Running two daemons on the same root would fight over `.process.lock` and UI locks.

Choose one of two safe resolutions:

- **Option A — Keep and reuse your existing Label (recommended for existing setups):**
  ```bash
  export GEMINI_TASK_BRIDGE_LABEL="<existing-label>"
  gemini-task-bridge service install
  ```
  (Or pass `--label "<existing-label>"` when running `install.sh`.)

- **Option B — Migrate to the default `io.gemini-task-bridge.daemon` Label:**
  ```bash
  launchctl bootout "gui/$(id -u)/<existing-label>"
  rm "$HOME/Library/LaunchAgents/<existing-label>.plist"
  gemini-task-bridge service install
  gemini-task-bridge doctor
  ```

---

## 4. Controlled Production Cutover (Upgrading Legacy `v2.0` Daemon)

When upgrading an active `v2.0` LaunchAgent running production T3 pairs, never write `local` pairs while the `v2.0` daemon is still active or unloaded-with-KeepAlive. Follow this provable barrier (substitute your actual `<label>` from `service status`):

1. Record `prior_mode` (`status.globalMode`) and `old_pid` (`daemonHealth.pid`).
2. Pause external CLI/MCP task submissions and run `gemini-task-bridge mode t3-all` (note timestamp `t_switch`).
3. Poll `gemini-task-bridge status --json` until the **same `old_pid`** emits a fresh heartbeat showing `"globalMode": "t3_only"` and `lastScanAt > t_switch`. This proves the preceding `hybrid` `scan()` (including any slow AX submit) has returned.
4. Wait until `counts.sending == 0`, `counts.outbox == 0`, `counts["inbox/in_flight"] == 0`, and no `locks/direct_t3.*.lock` holders remain.
5. Unload the old LaunchAgent completely (`launchctl bootout "gui/$(id -u)/<label>"`) and verify `.process.lock` is released (do not rely on `kickstart -k` returning early).
6. Run `bash scripts/install.sh` (atomic temp-file + `mv`; preserves existing state and writes both `gemini_task_bridge.py` and the `t3_gemini_bridge.py` compat copy).
7. Bootstrap/install the updated LaunchAgent (`launchctl bootstrap ...` or `gemini-task-bridge service install`) and verify `gemini-task-bridge doctor --json` shows **new `pid != old_pid`**, `"local_adapter"` and `"daemon_control_plane"` in `daemonHealth.capabilities`, fresh `lastScanAt`, `axTrusted == true`, and `ready == true`.
8. Restore `prior_mode` (`gemini-task-bridge mode hybrid`) before creating `local` pairs or starting `mcp`.

### History-Preserving Rollback (to `v2.0`)

1. Record `prior_mode` and `cur_pid`, pause external submissions, run `mode t3-all`, and observe `cur_pid` heartbeat showing `globalMode == "t3_only"`.
2. Drain `sending`, `outbox`, `inbox/in_flight`, **all `dispatchStates.dispatching`**, and wait for all active CLI/MCP `locks/direct_t3.*.lock` (`direct_t3_sending`) callers to finish.
3. Stop the LaunchAgent (`launchctl bootout ...` or `service uninstall`).
4. Snapshot `$ROOT` to `$ROOT/backups/<timestamp>/` (`0o700`). Export and remove all `adapter == "local"` entries (including disabled) from `pairs.json` and move unconsumed `local` `inbox/*.task.json` / `._stage.*` files to backup—**keeping all `sent/`, `sent/payloads/`, `inbox/done/`, and v2.0-compatible `ledger.json` history intact**.
5. Atomically restore `v2.0` scripts. (If you used `service uninstall`, which removes the `.plist`, re-install/bootstrap the LaunchAgent rather than `service restart`.)
6. Verify `v2.0` `doctor --json` health and restore `prior_mode`.

---

## 5. Gemini Desktop Workspace Folder Behavior

When creating a new Task conversation, `Gemini.app` automatically attaches its default folder (often `Documents`) as a folder chip in the composer, hiding the **Folders** dropdown button until that chip is removed.

`create_pair_with_ax` (used by both background control-plane `gemini_pair_init` / `pair init` and `--direct` mode) validates all parameters first, then drives the UI sequence:
1. Clicks **New chat** and ensures **Task** mode (`chat-input-mode-assign`) is active.
2. If the target folder chip is not already attached, clicks **Remove** / `移除` on the default folder chip so the **Folders** / `文件夹` menu button becomes visible.
3. Opens the **Folders** menu and selects the matching bookmarked path.

**If creation fails with `workspace_not_bookmarked_in_gemini`**:
`Gemini.app` only lists folders it has opened at least once via the native macOS folder picker. Open `Gemini.app`, start a temporary Task chat, click **Folders → Add new folder...**, select the workspace directory once, then re-run `gemini_pair_init` / `pair init`.

---

## 6. Troubleshooting Playbooks

All v2.2 CLI and MCP errors return `{"ok": false, "error": "<code>", "message": "...", "fix": "..."}` in Chinese or English (`GEMINI_TASK_BRIDGE_LANG=zh|en`, falling back to `LANG`/`LC_ALL`, default `en`). Always check `message` and `fix` first.

### `control_plane_unavailable`
- The daemon is stopped, its heartbeat is stale (`> 3 × poll_interval + 10s`), it lacks `"daemon_control_plane"` (pre-v2.2 daemon), or `daemonHealth.axTrusted == false`.
- Run `gemini-task-bridge doctor` and follow `next` (upgrade scripts via `install.sh`, grant `Python.app` Accessibility, and restart the service).

### `control_request_timeout` (CLI exit `3` / MCP `completed: false`)
- Not a failure—the daemon is still executing or waiting on the UI lock.
- For MCP `gemini_pair_init`: re-call with the exact same arguments (`idempotency_key` deduplicates automatically).
- For CLI: run `gemini-task-bridge pair request <requestId> --wait 60 --json` or re-run with the same `--idempotency-key`.

### `control_request_unconfirmed`
- The daemon crashed or restarted while `create_pair` was `in_flight` and could not prove whether `Gemini.app` finished creating the conversation.
- Open `Gemini.app` and check the sidebar: if the chat exists, bind it with `gemini-task-bridge pair add --id <id> --adapter local --gemini-title "<title>"`; otherwise retry creation with a fresh `--idempotency-key`. Never blindly auto-retry without checking.

### `pair_limit_reached`
- The MCP scope already owns 10 enabled pairs (`ownerScope == scope`).
- Reuse an existing pair from `gemini_pairs`, or remove unused pairs via `gemini-task-bridge pair remove <pair-id>`.

### `invalid_mcp_scope`
- `--scope` (or `GEMINI_TASK_BRIDGE_MCP_SCOPE`) did not match `^[a-z0-9][a-z0-9_-]{0,31}$`.
- Update the host MCP configuration to a lowercase slug (e.g., `claude-code`, `codex`, `antigravity`) and open a new session.

### `ax_not_trusted` / `daemonHealth.axTrusted == false`
- Check which executable the LaunchAgent uses:
  ```bash
  plutil -p ~/Library/LaunchAgents/"${GEMINI_TASK_BRIDGE_LABEL:-io.gemini-task-bridge.daemon}.plist" | grep -A 3 ProgramArguments
  ```
- Re-run `gemini-task-bridge service install` if you upgraded Python or Command Line Tools, ensure that `Python.app` is checked in **Accessibility**, then run `gemini-task-bridge service restart` and `gemini-task-bridge doctor`.

### `gemini_not_running` / `screen_locked`
- `gemini_not_running`: Launch `/Applications/Gemini.app` and re-run `doctor`.
- `screen_locked`: Unlock the Mac; queued tasks and control requests drain automatically on the next scan.

### `duplicate_gemini_title_in_pairs` / `pair_has_active_task` / `unknown_pair_id`
- `duplicate_gemini_title_in_pairs`: Another enabled pair already owns that exact `geminiTitle`; pick a distinct title or reuse the existing pair.
- `pair_has_active_task`: Wait for the pair's current task to finish (`task wait` / `gemini_wait`) before modifying or removing the pair.
- `unknown_pair_id`: Verify the ID exists and is visible to your MCP `--scope` / `--allow-pairs` via `gemini_pairs` or `pair list --json`.

### `daemon_upgrade_required_for_v21_features`
- An active `v2.0` daemon (or unloaded-with-KeepAlive `v2.0` plist / legacy `.daemon_health.json`) exists on the target `--root`. Follow Section 4 above before creating `local` pairs or running `mcp`.

### `needs_attention` (or CLI exit `3`)
- `Gemini.app` is displaying an inline confirmation/elicitation card (`ActionCardRemyConfirmationCard`, `ActionCardRemyElicitationCard`, `AgentWaitingForInputRow`, etc.) or `Needs attention` / `需要注意` sidebar badge.
- Ask the user to review and approve/reject inside `Gemini.app`, then resume waiting with `task wait <taskId>` / `gemini_wait`. Do **not** resend.

### `unknown_timeout`, `dispatch_submit_outcome_unknown`, or files in `failed/`
- Triggered when external `t3 thread send` timed out/exited non-zero, a `direct_t3` caller crashed, or an AX submit outcome was unconfirmed/uncertain and no `[[T3_BRIDGE_START:<taskId>]]` sentinel or receipt appeared.
- Inspect `gemini-task-bridge task status <taskId> --json`, `<root>/logs/bridge.log`, and the paired conversation before deciding whether to re-dispatch with a fresh `taskId`.

### `reserved_task_id_suffix`
- Custom `--task-id` values ending in `.receipt` (matched case-insensitively across macOS APFS volumes) are rejected on new intake to avoid flat `sent/` filename ambiguity. Pre-existing legacy tasks continue finishing normally.
