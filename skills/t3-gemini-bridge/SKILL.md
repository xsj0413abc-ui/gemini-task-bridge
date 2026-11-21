---
name: t3-gemini-bridge
description: Bridge T3 Code threads 1-to-1 with Google Gemini Desktop App (Gemini.app) Task conversations on macOS using native Accessibility (AXUIElement) and the official t3 CLI. Use whenever the user wants to install or diagnose t3-gemini-bridge, bind a T3 thread to a Gemini Desktop Task chat, dispatch prompts automatically without manual wake-up, receive automatic dual-channel replies back into T3, troubleshoot AX/TCC/needs_attention issues, or instantly switch routing back to T3-only (mode t3-all or pair route). Do not use for configuring Gemini CLI, Antigravity API/ACP providers inside T3 Settings, or non-macOS hosts.
license: MIT
metadata:
  short-description: 1-to-1 bridge between T3 Code and Gemini Desktop Task mode
  platform: macos
  version: "2.0.0"
---

# T3 Code <-> Gemini Desktop Bridge

Operate and maintain a zero-dependency, 1-to-1 bidirectional bridge between T3 Code threads and Google Gemini Desktop (`Gemini.app`) Task conversations on macOS.

## Prerequisites & CLI Discovery

Locate the installed bridge CLI before running operational commands:

1. Check `command -v t3-gemini-bridge` or `~/.local/bin/t3-gemini-bridge`.
2. If not installed yet, run the bundled installer at `scripts/install.sh` (or run `python3 -B scripts/t3_gemini_bridge.py` directly for inspection).

Never mutate T3 Code's SQLite database (`statev2.sqlite`), copy internal provider session tokens, or write directly to Gemini Desktop's SwiftData store (`custom-local-mcp-servers.store`). The bridge operates strictly through `Gemini.app` Accessibility (`AXUIElement`) and the official `t3 thread send` CLI.

---

## Step 0: 10-Second Preflight

Run a read-only check before dispatching tasks or modifying pairs:

```bash
t3-gemini-bridge status --json
t3-gemini-bridge doctor --json
```

All 4 green-light conditions must hold for automatic Gemini dispatch and capture:

1. `launchAgent.state == "running"` (background daemon is alive)
2. `daemonHealth.axTrusted == true` (macOS Accessibility granted to the LaunchAgent executable)
3. `daemonHealth.screenLocked == false` (macOS session is unlocked)
4. `daemonHealth.geminiRunning == true` (`Gemini.app` is running)

---

## Workflow 1 — First-Time macOS Setup

Run the self-contained installer when setting up on a new Mac or upgrading the runtime scripts (it never overwrites existing `pairs.json`, `bridge_config.json`, or `ledger.json`):

```bash
bash scripts/install.sh
```

Then complete macOS TCC authorization before installing the LaunchAgent:

1. **Grant Accessibility to `Python.app` (not `/usr/bin/python3`)**: On macOS, `/usr/bin/python3` shares an inode via hardlinks with dozens of developer shims (including `/usr/bin/git`), causing macOS `tccd` to misattribute LaunchAgent permissions across restarts. `install.sh` and `service install` resolve a dedicated single-link `Python.app` binary (printed as `TCC Python` by `install.sh` and `pythonExecutable` by `service install`). Enable that exact `Python.app` in **System Settings -> Privacy & Security -> Accessibility**.
2. **Grant Full Disk Access (when needed)**: If your paired Gemini workspaces live in protected folders such as `~/Desktop` or `~/Documents`, grant Full Disk Access to the same `Python.app` (and `Gemini.app` if prompted).
3. **Install the LaunchAgent and verify**:

```bash
t3-gemini-bridge service install
t3-gemini-bridge doctor --json
```

Ensure `~/.local/bin` is on `PATH` and `t3` CLI is executable (`~/.t3/bin/t3` or on `PATH`). For custom roots/labels/CLI paths, see [references/cli-reference.md](references/cli-reference.md).

---

## Workflow 2 — Create or Bind a 1-to-1 Pair

Every enabled entry in `pairs.json` enforces strict 1-to-1 uniqueness: no two enabled pairs may share the same `t3ThreadId` or `geminiTitle`.

### Option A: Create a fresh Gemini Task conversation (Recommended)

`pair init` opens a new chat in `Gemini.app`, switches to Task mode, removes Gemini's default `Documents` folder chip if `--workspace` is specified, attaches the target folder, waits for Gemini's auto-title, and renames it to `--gemini-title`:

```bash
t3-gemini-bridge pair init \
  --id "reviewer" \
  --label "Code Reviewer" \
  --t3-thread "<target-t3-thread-id>" \
  --reply-to "<orchestrator-t3-thread-id>" \
  --gemini-title "Code Reviewer" \
  --workspace "/abs/path/to/workspace"
```

- **Hub-and-spoke vs self-reply**: Pass `--reply-to <orchestrator-thread-id>` when an orchestrator/director thread delegates work and expects results back in the orchestrator thread. Omit `--reply-to` to default replies back to `--t3-thread`.
- **Workspace bookmark prerequisite**: `Gemini.app` only exposes previously opened/bookmarked folders in its Folder picker menu. If `pair init` reports `workspace_not_bookmarked_in_gemini`, open that folder once in Gemini Desktop's folder menu and retry.

### Option B: Bind an existing Gemini Desktop conversation

Inspect exact sidebar/menu titles first, then bind:

```bash
t3-gemini-bridge pair gemini-chats --json
t3-gemini-bridge pair add \
  --id "reviewer" \
  --t3-thread "<target-t3-thread-id>" \
  --reply-to "<orchestrator-t3-thread-id>" \
  --gemini-title "<exact-gemini-title>"
```

To inspect or remove bindings without deleting any T3 or Gemini conversation history:

```bash
t3-gemini-bridge pair list --json
t3-gemini-bridge pair remove "<pair-id>"
```

---

## Workflow 3 — Automatic Dispatch & Dual-Channel Return (`send`)

Once a thread is managed by a pair, **always dispatch via `t3-gemini-bridge send`** rather than calling `t3_thread_send` directly. Bypassing `send` breaks global/per-pair mode routing (`mode t3-all` / `pair route`).

```bash
# Async dispatch (returns taskId immediately; daemon delivers reply when done)
t3-gemini-bridge send \
  --pair "<pair-id>" \
  --message "Inspect /abs/path/to/file.mp4 and report key frame issues."

# Read long instructions from a file or stdin (max 32,768 UTF-8 bytes / 400 lines)
t3-gemini-bridge send --pair "<pair-id>" --file /abs/path/to/task.md

# Synchronous wait for reply receipt (e.g. 300s timeout)
t3-gemini-bridge send --pair "<pair-id>" --wait --timeout 300 --json --message "..."
```

Key authoring rules for dispatched prompts:

- **Never hand-write bridge sentinels or `outbox/*.json` instructions**: `send` wraps messages with split-literal sentinels (`[[T3_BRIDGE_START:<taskId>]]` and split `[[T3_BRIDGE_DONE:` + `<taskId>]]`) automatically so prompt echoes cannot prematurely trigger completion.
- **Always use absolute paths** for every file, workspace artifact, or reference mentioned in `--message`.
- **Execution concurrency**: Tasks for the same `pairId` run strictly FIFO (one `in_flight` at a time to prevent context bleed); tasks across different `pairId`s run concurrently.
- **Dual-channel return**: Gemini may either write a structured JSON receipt to `<root>/outbox/<taskId>.json` (primary) or reply normally in chat (fallback AX capture after stability debounce). Both are delivered via `t3 thread send` to `replyThreadId`.

---

## Workflow 4 — Instant Rollback & Per-Pair Routing

Switch all paired workflows back to native T3 threads (or selectively per pair) at any time without restarting the daemon or touching existing conversations:

```bash
# Instant global rollback to T3-only (all `send` calls route straight to pair.t3ThreadId)
t3-gemini-bridge mode t3-all

# Restore per-pair hybrid routing
t3-gemini-bridge mode hybrid

# Switch a single pair without affecting others
t3-gemini-bridge pair route "<pair-id>" t3
t3-gemini-bridge pair route "<pair-id>" gemini

# Switch global mode to hybrid and set all enabled pairs to gemini
t3-gemini-bridge mode gemini-all
```

**Cross-client memory handoff**: T3 Code threads and Gemini Desktop Task chats maintain separate conversation histories. When switching a workflow between Gemini and T3, include authoritative file paths (status docs, artifacts, versions) in the next dispatched prompt so the receiving side picks up from disk state rather than stale chat memory.

---

## Workflow 5 — Handling Exceptions Safely

| Symptom / Status Field | Meaning | Required Action |
| --- | --- | --- |
| `dispatchStates.needs_attention > 0` | Gemini Desktop is showing a confirmation or permission card | Ask the user to confirm in `Gemini.app`; bridge resumes automatically afterward. **Do not resend.** |
| `daemonHealth.axTrusted == false` | LaunchAgent lost macOS Accessibility trust | Run `mode t3-all` if work is urgent; have user enable the resolved `Python.app` in System Settings -> Accessibility. |
| `daemonHealth.screenLocked == true` | Mac display is locked; GUI dispatch paused safely | Wait for unlock; `inbox/` drains automatically on next scan. |
| `unknown_timeout` or `failed` | `t3 thread send` timed out or failed; message may already be queued in T3 | **Never blindly retry with a new `taskId`.** Check `sent/` and read the target T3 thread first to confirm delivery state. |
| Files in `dedup/` | Duplicate `taskId` receipt was intercepted and suppressed | Normal idempotency protection; no action needed. |
| `title_verification_failed` in logs | Gemini window title did not match `geminiTitle` after navigation | Run `pair gemini-chats --json` to check if the chat was renamed, then re-bind or match the title. |

Before restarting the background daemon (`t3-gemini-bridge service restart`), run `t3-gemini-bridge status --json` and confirm `counts.sending == 0` to avoid interrupting an active CLI delivery.

---

## Supporting References

Read these only when the current task requires deep details:

- **Full CLI flags, env vars, JSON schemas, exit codes, size limits**: read [references/cli-reference.md](references/cli-reference.md).
- **macOS `Python.app` TCC hardening, folder bookmarking, and recovery playbooks**: read [references/macos-permissions-and-troubleshooting.md](references/macos-permissions-and-troubleshooting.md).
- **Protocol internals, 4-layer anti-crosstalk, split-sentinel slicing, and security invariants**: read [references/architecture-and-safety.md](references/architecture-and-safety.md).
