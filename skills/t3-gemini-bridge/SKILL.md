---
name: t3-gemini-bridge
description: Bridge Google Gemini Desktop App (Gemini.app) Task conversations 1-to-1 with T3 Code, Claude Code, Codex, and Antigravity on macOS using native Accessibility (AXUIElement), atomic task queues, and a zero-dependency stdio MCP server. Use when installing or diagnosing t3-gemini-bridge, binding local or T3 pairs to Gemini Task chats, dispatching tasks via CLI (`send`, `task status|wait|read`) or MCP (`gemini_send`, `gemini_wait`, `gemini_read`), resolving needs_attention confirmation cards, or switching routing modes (`mode t3-all` / `pair route`). Do not use for Gemini CLI, API keys inside provider settings, or non-macOS hosts.
license: MIT
metadata:
  short-description: 1-to-1 bridge to Gemini Desktop Task mode for T3 Code, Codex, Claude Code, and Antigravity
  platform: macos
  version: "2.1.0"
---

# Gemini Desktop Bridge (`t3-gemini-bridge`)

Operate and maintain a zero-dependency, 1-to-1 bridge between Google Gemini Desktop (`Gemini.app`) Task conversations and **T3 Code**, **Claude Code**, **Codex**, or **Antigravity** on macOS.

## Prerequisites & CLI Discovery

Locate the installed bridge CLI before running operational commands:

1. Check `command -v t3-gemini-bridge` or `~/.local/bin/t3-gemini-bridge`.
2. If not installed yet, run `bash scripts/install.sh` (or run `python3 -B scripts/t3_gemini_bridge.py` directly for inspection).

Never mutate T3 Code's SQLite database (`statev2.sqlite`), copy internal provider session tokens, or write directly to Gemini Desktop's SwiftData store (`custom-local-mcp-servers.store`). The bridge operates strictly through `Gemini.app` Accessibility (`AXUIElement`), atomic local archives (`sent/payloads/`, `sent/`), an optional stdio MCP server (`t3-gemini-bridge mcp`), and (for `adapter=t3` pairs only) the official `t3 thread send` CLI.

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

Note: `doctor --json` reports `"t3CliRequired": true` only when at least one enabled `adapter="t3"` pair exists. Pure `local` / MCP setups (Claude Code, Codex, Antigravity) work without installing `t3`.

---

## Workflow 1 — First-Time macOS Setup (or Safe Upgrade)

Run the self-contained installer when setting up on a new Mac or upgrading the runtime scripts (uses atomic temp-file + `mv`, never overwriting existing `pairs.json`, `bridge_config.json`, `ledger.json`, or queue directories):

```bash
bash scripts/install.sh
```

Then complete macOS TCC authorization before installing the LaunchAgent:

1. **Grant Accessibility to `Python.app` (not `/usr/bin/python3`)**: On macOS, `/usr/bin/python3` shares an inode via hardlinks with dozens of developer shims (including `/usr/bin/git`), causing macOS `tccd` to misattribute LaunchAgent permissions across restarts. `install.sh` and `service install` resolve a dedicated single-link `Python.app` binary (printed as `TCC Python` by `install.sh` and `pythonExecutable` by `service install`). Enable that exact `Python.app` in **System Settings -> Privacy & Security -> Accessibility**.
2. **Grant Full Disk Access (when needed)**: If your paired Gemini workspaces live in protected folders such as `~/Desktop` or `~/Documents`, grant Full Disk Access to the same `Python.app` (and `Gemini.app` if prompted).
3. **Install and verify the LaunchAgent**:

```bash
t3-gemini-bridge service install
t3-gemini-bridge doctor --json
```

Upgrading an older `v2.0` LaunchAgent that already runs T3 production pairs? Follow the controlled cutover procedure (`mode t3-all` -> observe old-PID `globalMode == "t3_only"` heartbeat -> drain queues -> `bootout` -> `install.sh` -> `bootstrap` -> verify `local_adapter` capability -> `mode hybrid`) in [references/macos-permissions-and-troubleshooting.md](references/macos-permissions-and-troubleshooting.md) before creating `local` pairs.

---

## Workflow 2 — Create or Bind a 1-to-1 Pair

Every enabled pair enforces strict 1-to-1 `geminiTitle` uniqueness across all pairs (`local` + `t3`). Enabled `t3` pairs also enforce unique `t3ThreadId`s.

### Mode A: Universal Local / MCP Pair (Claude Code, Codex, Antigravity — No `t3` CLI Required)

**Create a fresh Gemini Task conversation (Recommended):**

```bash
t3-gemini-bridge pair init \
  --id "codex_reviewer" \
  --adapter local \
  --label "Codex Reviewer" \
  --gemini-title "Codex Reviewer" \
  --workspace "/abs/path/to/workspace"
```

**Or bind an existing Gemini Desktop chat:**

```bash
t3-gemini-bridge pair gemini-chats --json
t3-gemini-bridge pair add \
  --id "codex_reviewer" \
  --adapter local \
  --gemini-title "<exact-gemini-title>"
```

### Mode B: T3 Code Bidirectional Pair (`adapter=t3`)

```bash
t3-gemini-bridge pair init \
  --id "reviewer" \
  --label "Code Reviewer" \
  --t3-thread "<target-t3-thread-id>" \
  --reply-to "<orchestrator-t3-thread-id>" \
  --gemini-title "Code Reviewer" \
  --workspace "/abs/path/to/workspace"
```

- **Hub-and-spoke vs self-reply**: Pass `--reply-to <orchestrator-thread-id>` when an orchestrator thread delegates work and expects results back in the orchestrator thread. Omit `--reply-to` to default replies back to `--t3-thread`.
- **Workspace bookmark prerequisite**: `Gemini.app` only exposes previously opened/bookmarked folders in its Folder picker menu. If `pair init` fails with `workspace_not_bookmarked_in_gemini`, open that folder once in Gemini Desktop's folder menu and retry.

To inspect or remove bindings without deleting any chat history:

```bash
t3-gemini-bridge pair list --json
t3-gemini-bridge pair remove "<pair-id>"
```

---

## Workflow 3 — Dispatch, Track & Read Replies

### Option 1: Zero-Dependency Stdio MCP Server (Recommended for Claude Code, Codex, Antigravity)

Start the scoped MCP server (defaults to `--adapter-filter local` so production `t3` pairs remain isolated):

```bash
t3-gemini-bridge mcp --adapter-filter local --allow-pairs "codex_reviewer"
```

Exposed minimal-privilege tools: `gemini_health`, `gemini_pairs`, `gemini_send` (non-blocking by default, returns durable `taskId`), `gemini_wait` (chunked polling up to 300s, returns `isError: false` + `completed: false` on chunk expiry or `needs_attention`), and `gemini_read`. See [references/mcp-and-multi-agent.md](references/mcp-and-multi-agent.md) for host-native Codex TOML, Claude Code `.mcp.json`, and Antigravity setup.

### Option 2: CLI Dispatch & Task Subcommands

```bash
# Async dispatch (returns taskId immediately)
t3-gemini-bridge send \
  --pair "<pair-id>" \
  --idempotency-key "req-001" \
  --message "Inspect /abs/path/to/file.mp4 and report key frame issues."

# Inspect status, wait in bounded chunks (exit 3 on attention if requested), or read verified reply
t3-gemini-bridge task status "<taskId>" --json
t3-gemini-bridge task wait "<taskId>" --timeout 120 --return-on-attention --json
t3-gemini-bridge task read "<taskId>" --json

# Synchronous wait from send (returns receipt + verified `message` on exit 0)
t3-gemini-bridge send --pair "<pair-id>" --wait --timeout 300 --json --message "..."
```

Key rules for dispatched prompts:

- **Never hand-write bridge sentinels or `outbox/*.json` instructions**: `send` wraps messages with split-literal sentinels (`[[T3_BRIDGE_START:<taskId>]]` and split `[[T3_BRIDGE_DONE:` + `<taskId>]]`) automatically.
- **Reserved ID suffix**: Custom `--task-id` values ending in `.receipt` (case-insensitive) are rejected (`reserved_task_id_suffix`) to preserve flat `sent/` archive compatibility.
- **Always use absolute paths** for every file or artifact mentioned in `--message`.
- **Execution concurrency**: Tasks for the same `pairId` run strictly FIFO; tasks across different `pairId`s run concurrently.
- **Dual-channel return**: Gemini may write `<root>/outbox/<taskId>.json` or reply in chat (fallback AX capture). `t3` pairs deliver via `t3 thread send` and archive flat `sent/<taskId>.json` + `sent/<taskId>.receipt.json`; `local` pairs archive cleanly to `sent/payloads/<taskId>.json` + `sent/<taskId>.receipt.json`.

---

## Workflow 4 — Instant Rollback & Per-Pair Routing

```bash
# Global emergency brake: `t3` pairs route to T3; new `local` sends fail closed (exit 2); queued `local` pauses safely
t3-gemini-bridge mode t3-all

# Restore per-pair hybrid routing
t3-gemini-bridge mode hybrid

# Switch a single `t3` pair without affecting others (`local` pairs cannot route to T3)
t3-gemini-bridge pair route "<t3-pair-id>" t3
t3-gemini-bridge pair route "<t3-pair-id>" gemini
```

---

## Workflow 5 — Handling Exceptions Safely

| Symptom / Status Field | Meaning | Required Action |
| --- | --- | --- |
| `needs_attention` (or CLI exit `3`) | Gemini Desktop is showing a confirmation or permission card | Ask the user to confirm in `Gemini.app`; call `task wait` / `gemini_wait` with the same `taskId`. **Do not resend.** |
| `daemon_upgrade_required_for_v21_features` | Running LaunchAgent is an older `v2.0` daemon (or KeepAlive risk exists) | Follow the controlled Cutover SOP in [references/macos-permissions-and-troubleshooting.md](references/macos-permissions-and-troubleshooting.md). |
| `daemonHealth.axTrusted == false` | LaunchAgent lost macOS Accessibility trust | Enable the resolved `Python.app` in System Settings -> Accessibility and restart daemon. |
| `daemonHealth.screenLocked == true` | Mac display is locked; GUI dispatch paused safely | Wait for unlock; `inbox/` drains automatically on next scan. |
| `unknown_timeout` or `failed` | External `t3` delivery timed out/crashed or AX `dispatching` outcome could not be confirmed | **Never blindly retry with a new `taskId`.** Check `task status`, `sent/`, and the target chat first. |
| `title_verification_failed` in logs | Gemini window title did not match `geminiTitle` | Run `pair gemini-chats --json` to check if the chat was renamed. |

---

## Supporting References

- **Full CLI flags, exit codes (`0/1/2/3`), schemas, size limits**: [references/cli-reference.md](references/cli-reference.md)
- **MCP server tools, Codex / Claude Code / Antigravity host-native configs, Stage A/B verification**: [references/mcp-and-multi-agent.md](references/mcp-and-multi-agent.md)
- **macOS `Python.app` TCC hardening, Cutover & Rollback SOPs, troubleshooting**: [references/macos-permissions-and-troubleshooting.md](references/macos-permissions-and-troubleshooting.md)
- **Protocol internals, 3-phase single-link intake, pre-AX `dispatching`, security & ACL invariants**: [references/architecture-and-safety.md](references/architecture-and-safety.md)
