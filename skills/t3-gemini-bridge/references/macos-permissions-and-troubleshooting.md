# macOS Permissions, TCC Hardening & Troubleshooting

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
   - Also enable your terminal app (e.g., Terminal, iTerm2, Ghostty, or T3 Code) if you invoke `pair init` or `pair gemini-chats` interactively from a shell.
2. **Full Disk Access** (**System Settings -> Privacy & Security -> Full Disk Access**):
   - Required if paired Gemini workspaces or task files live under TCC-protected folders (`~/Desktop`, `~/Documents`, `~/Downloads`).
3. Restart the daemon after toggling TCC permissions so the process picks up the updated trust token:
   ```bash
   t3-gemini-bridge status --json   # Verify counts.sending == 0 first
   t3-gemini-bridge service restart
   t3-gemini-bridge doctor --json
   ```

---

## 2. Gemini Desktop Workspace Folder Behavior

When creating a new Task conversation, `Gemini.app` automatically attaches its default folder (often `Documents`) as a folder chip in the composer, hiding the **Folders** dropdown button until that chip is removed.

`pair init --workspace <abs-dir>` automates the exact UI sequence:
1. Clicks **New chat** and ensures **Task** mode (`chat-input-mode-assign`) is active.
2. If the target folder chip is not already attached, clicks **Remove** / `移除` on the default folder chip so the **Folders** / `文件夹` menu button becomes visible.
3. Opens the **Folders** menu and selects the matching bookmarked path.

**If `pair init` fails with `workspace_not_bookmarked_in_gemini`**:
`Gemini.app` only lists folders it has opened at least once via the native macOS folder picker. Open `Gemini.app`, start a temporary Task chat, click **Folders -> Add new folder...**, select the workspace directory once, then re-run `pair init`.

---

## 3. Troubleshooting Playbooks

### `daemonHealth.axTrusted == false`
- Check which executable the LaunchAgent uses:
  ```bash
  plutil -p ~/Library/LaunchAgents/"${T3_GEMINI_BRIDGE_LABEL:-io.t3-gemini-bridge.daemon}.plist" | grep -A 3 ProgramArguments
  ```
- Re-run `t3-gemini-bridge service install` if you upgraded Python or Command Line Tools, ensure that `Python.app` is checked in **Accessibility**, then run `service restart` and `doctor --json`.
- While resolving permissions, run `t3-gemini-bridge mode t3-all` so T3 workflows continue uninterrupted.

### `dispatchStates.needs_attention > 0`
- `Gemini.app` is displaying an inline confirmation/elicitation card (`ActionCardRemyConfirmationCard`, `ActionCardRemyElicitationCard`, `AgentWaitingForInputRow`, etc.).
- The bridge intentionally pauses AX capture and subsequent dispatches for that conversation and posts a macOS notification.
- Have the user review and approve/reject inside `Gemini.app`. Do **not** resend the task; polling resumes automatically once confirmed.

### `unknown_timeout` or files in `<root>/failed/`
- Happens if `t3 thread send` timed out (60s) or exited non-zero, or if the daemon restarted mid-send (`recovered_interrupted_send_no_retry`).
- Because the message may already have reached T3 before the timeout or crash, the bridge **refuses to auto-retry** (preventing duplicate agent turns).
- Recovery steps:
  1. Check the target T3 thread (via `t3 thread show <threadId>` or `t3_thread_read`) to see whether the reply arrived.
  2. Inspect `<root>/failed/<taskId>.json` and `<root>/logs/bridge.log`.
  3. If the reply truly never arrived, inspect the source Gemini conversation and forward or re-dispatch cleanly with a fresh `taskId`.

### `title_verification_failed` or `conversation_not_found`
- Run `t3-gemini-bridge pair gemini-chats --json` to list exact titles currently visible in Gemini's sidebar and **Chats** menu.
- If the conversation was manually renamed or deleted in `Gemini.app`, update the pair with `t3-gemini-bridge pair add --id <pairId> ...` using the current exact title, or create a fresh one via `pair init`.

### Safe Upgrades Without Data Loss
- Re-running `bash scripts/install.sh` updates `t3_gemini_bridge.py`, `bridge_ax.py`, and the `t3-gemini-bridge` wrapper while preserving `bridge_config.json`, `pairs.json`, `ledger.json`, and all queue directories.
- Always check `t3-gemini-bridge status --json` (`counts.sending == 0`) before calling `t3-gemini-bridge service restart`.
