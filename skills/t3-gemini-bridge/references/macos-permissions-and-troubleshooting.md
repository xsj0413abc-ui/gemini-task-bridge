# macOS Permissions, Cutover/Rollback & Troubleshooting

## 1. Why LaunchAgent Binds `Python.app` Instead of `/usr/bin/python3`

On macOS, `/usr/bin/python3` is an Apple developer tool shim that shares the exact same filesystem inode (typically ~78 hardlinks) with `/usr/bin/git`, `/usr/bin/clang`, and other Xcode Command Line Tools binaries.

When a `launchd` LaunchAgent executes `/usr/bin/python3` in the background, macOS `tccd` (Transparency, Consent, and Control) resolves the responsible binary via inode/code-identity heuristics and frequently misattributes the process as `/usr/bin/git`. This breaks `AXIsProcessTrusted()` silently after reboots or agent restarts even when `/usr/bin/python3` appears checked in System Settings.

`resolve_launchagent_python()` prevents this by probing for a dedicated single-hardlink (`st_nlink == 1`) `Python.app` binary in this order:

1. `<sys.base_prefix>/Resources/Python.app/Contents/MacOS/Python`
2. `/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/*/Resources/Python.app/Contents/MacOS/Python` (preferring the active Python minor version, then newest installed version)
3. `/Library/Frameworks/Python.framework/Versions/*/Resources/Python.app/Contents/MacOS/Python`, `/opt/homebrew/Frameworks/...`, and `/usr/local/Frameworks/...`
4. `os.path.realpath(sys.executable)` if `st_nlink == 1`

### Required macOS Permissions

1. **Accessibility** (**System Settings -> Privacy & Security -> Accessibility**):
   - Add and enable the exact `Python.app` reported by `install.sh` (`TCC Python`) or `t3-gemini-bridge service install` (`pythonExecutable`).
   - Also enable your terminal app or host app if you invoke `pair init` or `pair gemini-chats` interactively from a shell.
2. **Full Disk Access** (**System Settings -> Privacy & Security -> Full Disk Access**):
   - Required if paired Gemini workspaces or task files live under TCC-protected folders (`~/Desktop`, `~/Documents`, `~/Downloads`).
3. Restart the daemon after toggling TCC permissions so the process picks up the updated trust token.

---

## 2. Controlled Production Cutover (`v2.0 -> v2.1`)

When upgrading an active LaunchAgent running production T3 pairs, never write `local` pairs while the `v2.0` daemon is still active or unloaded-with-KeepAlive. Follow this provable barrier:

1. Record `prior_mode` (`status.globalMode`) and `old_pid` (`daemonHealth.pid`).
2. Pause external CLI/MCP task submissions and run `t3-gemini-bridge mode t3-all` (note timestamp `t_switch`).
3. Poll `t3-gemini-bridge status --json` until the **same `old_pid`** emits a fresh heartbeat showing `"globalMode": "t3_only"` and `lastScanAt > t_switch`. This proves the preceding `hybrid` `scan()` (including any slow AX submit) has returned.
4. Wait until `counts.sending == 0`, `counts.outbox == 0`, `counts["inbox/in_flight"] == 0`, and no `locks/direct_t3.*.lock` holders remain.
5. Unload the old LaunchAgent completely (`launchctl bootout gui/$(id -u)/io.t3-gemini-bridge.daemon`) and verify `.process.lock` is released (do not rely on `kickstart -k` returning early).
6. Run `bash scripts/install.sh` (atomic temp-file + `mv`).
7. Bootstrap/install the updated LaunchAgent (`launchctl bootstrap ...` or `t3-gemini-bridge service install`) and verify `t3-gemini-bridge doctor --json` shows **new `pid != old_pid`**, `"local_adapter" in daemonHealth.capabilities`, fresh `lastScanAt`, and `axTrusted == true`.
8. Restore `prior_mode` (`t3-gemini-bridge mode hybrid`) before creating `local` pairs or starting `mcp`.

---

## 3. History-Preserving Rollback (`v2.1 -> v2.0`)

1. Record `prior_mode` and `cur_pid`, pause external submissions, run `mode t3-all`, and observe `cur_pid` heartbeat showing `globalMode == "t3_only"`.
2. Drain `sending`, `outbox`, `inbox/in_flight`, **all `dispatchStates.dispatching`**, and wait for all active CLI/MCP `locks/direct_t3.*.lock` (`direct_t3_sending`) callers to finish.
3. Stop the LaunchAgent (`launchctl bootout ...` or `service uninstall`).
4. Snapshot `$ROOT` to `$ROOT/backups/<timestamp>/` (`0o700`). Export and remove all `adapter == "local"` entries (including disabled) from `pairs.json` and move unconsumed `local` `inbox/*.task.json` / `._stage.*` files to backup—**keeping all `sent/`, `sent/payloads/`, `inbox/done/`, and v2.0-compatible `ledger.json` history intact**.
5. Atomically restore `v2.0` scripts. (**Note**: if you used `service uninstall`, which removes the `.plist`, run `t3-gemini-bridge service install` / `launchctl bootstrap` rather than `service restart`.)
6. Verify `v2.0` `doctor --json` health and restore `prior_mode`.

---

## 4. Gemini Desktop Workspace Folder Behavior

When creating a new Task conversation, `Gemini.app` automatically attaches its default folder (often `Documents`) as a folder chip in the composer, hiding the **Folders** dropdown button until that chip is removed.

`pair init --workspace <abs-dir>` validates all parameters and runtime gates first, then drives the UI sequence:
1. Clicks **New chat** and ensures **Task** mode (`chat-input-mode-assign`) is active.
2. If the target folder chip is not already attached, clicks **Remove** / `移除` on the default folder chip so the **Folders** / `文件夹` menu button becomes visible.
3. Opens the **Folders** menu and selects the matching bookmarked path.

**If `pair init` fails with `workspace_not_bookmarked_in_gemini`**:
`Gemini.app` only lists folders it has opened at least once via the native macOS folder picker. Open `Gemini.app`, start a temporary Task chat, click **Folders -> Add new folder...**, select the workspace directory once, then re-run `pair init`.

---

## 5. Troubleshooting Playbooks

### `daemon_upgrade_required_for_v21_features`
- An active `v2.0` daemon (or unloaded-with-KeepAlive `v2.0` plist / legacy `.daemon_health.json`) exists on the target `--root`. Follow Section 2 above before creating `local` pairs or running `mcp`.

### `daemonHealth.axTrusted == false`
- Check which executable the LaunchAgent uses:
  ```bash
  plutil -p ~/Library/LaunchAgents/"${T3_GEMINI_BRIDGE_LABEL:-io.t3-gemini-bridge.daemon}.plist" | grep -A 3 ProgramArguments
  ```
- Re-run `t3-gemini-bridge service install` if you upgraded Python or Command Line Tools, ensure that `Python.app` is checked in **Accessibility**, then run `service restart` and `doctor --json`.

### `needs_attention` (or CLI exit `3`)
- `Gemini.app` is displaying an inline confirmation/elicitation card (`ActionCardRemyConfirmationCard`, `ActionCardRemyElicitationCard`, `AgentWaitingForInputRow`, etc.) or `Needs attention` sidebar badge.
- Ask the user to review and approve/reject inside `Gemini.app`, then resume waiting with `task wait <taskId>` / `gemini_wait`. Do **not** resend.

### `unknown_timeout`, `dispatch_submit_outcome_unknown`, or `files in failed/`
- Triggered when external `t3 thread send` timed out/exited non-zero, a `direct_t3` caller crashed, or an AX submit outcome was unconfirmed/uncertain and no `[[T3_BRIDGE_START:<taskId>]]` sentinel or receipt appeared.
- Inspect `t3-gemini-bridge task status <taskId> --json`, `<root>/logs/bridge.log`, and the paired conversation before deciding whether to re-dispatch with a fresh `taskId`.

### `reserved_task_id_suffix`
- Custom `--task-id` values ending in `.receipt` (matched case-insensitively across macOS APFS volumes) are rejected on new intake to avoid flat `sent/` filename ambiguity. Pre-existing legacy tasks continue finishing normally.
