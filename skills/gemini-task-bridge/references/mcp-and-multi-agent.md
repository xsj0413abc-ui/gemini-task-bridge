# Multi-Agent & Stdio MCP Guide (Claude Code / Codex / Antigravity)

`gemini-task-bridge mcp` provides a zero-dependency Python 3.9+ stdlib MCP stdio server (`2025-06-18`, `2025-03-26`, and `2024-11-05`) so **Claude Code**, **Codex**, and **Antigravity** can create dedicated `Gemini.app` Task conversations, dispatch tasks, and read verified replies—without installing T3 Code, without granting Accessibility to the AI host, and without touching existing T3 production pairs.

---

## 1. Security Model, `--scope` Isolation & Quota

- **Default adapter filter**: `--adapter-filter local` (T3 pairs are hidden by default; set `--adapter-filter all` or `t3` only when explicitly intended).
- **Per-host `--scope <name>`** (env `GEMINI_TASK_BRIDGE_MCP_SCOPE`, compat `T3_GEMINI_BRIDGE_MCP_SCOPE`):
  - Must match `^[a-z0-9][a-z0-9_-]{0,31}$` (e.g., `claude-code`, `codex`, `antigravity`). Invalid scopes fail closed at startup (`invalid_mcp_scope`, exit `2`).
  - **Visibility rule**:
    - When `--scope` is **set**: visible pairs = `{pair | pair.ownerScope == scope} ∪ --allow-pairs` (still filtered by `--adapter-filter`). Scope A cannot see or read tasks belonging to Scope B.
    - When `--scope` is **unset**: keeps legacy visibility (`--allow-pairs` if provided, otherwise all enabled pairs matching `--adapter-filter`).
- **Explicit pair allowlist (`--allow-pairs`)**:
  - Pass `--allow-pairs pair_a,pair_b` (or `GEMINI_TASK_BRIDGE_MCP_ALLOW_PAIRS`, compat `T3_GEMINI_BRIDGE_MCP_ALLOW_PAIRS`). Any empty string, whitespace, empty segment (`"a,"`), or invalid identifier **fails closed at startup** (`exit 2`).
- **AI pair-creation quota**:
  - Each scope may own at most **10 enabled pairs** (`ownerScope == scope`). Attempting to create an 11th pair via `gemini_pair_init` fails with `pair_limit_reached`.
- **Zero Accessibility in MCP**:
  - `gemini_list_chats` and `gemini_pair_init` exclusively submit requests to the daemon control queue (`control/inbox/`). The MCP server process **never** calls macOS Accessibility APIs directly.
- **3-Lane Concurrency**:
  - Control frames (`initialize`, `notifications/initialized`, `notifications/cancelled`, `ping`, `tools/list`) run directly on the reader thread with zero queue delay.
  - Non-blocking tools (`gemini_health`, `gemini_pairs`, `gemini_read`, `gemini_send(wait=false)`) run in a bounded fast pool (overflow returns `mcp_server_busy`).
  - Blocking waits (`gemini_wait`, `gemini_send(wait=true)`, `direct_t3`, control waits) are capped by a non-blocking semaphore (`max_concurrent_waits = 8`; a 9th concurrent wait fails fast with `too_many_concurrent_waits`).
- **OS Boundary Note**: Scope isolation enforces access boundaries at this MCP server protocol interface; it is not OS-level filesystem sandboxing between processes running under the same macOS user account.

---

## 2. MCP Tool Reference

All tool schemas declare snake_case parameter names; the server also accepts camelCase aliases (`pair_id`/`pairId`, `task_id`/`taskId`, `idempotency_key`/`idempotencyKey`). Every tool error payload includes `error`, plus localized `message` and `fix`.

| Tool | Purpose | Parameters | Key Behavior |
| --- | --- | --- | --- |
| `gemini_health` | Check LaunchAgent liveness, `daemonHealth.axTrusted`, screen lock, `Gemini.app`, and scoped counts | *(none)* | Reports only scoped counts and attention list; never leaks global pairs or counts. |
| `gemini_pairs` | List enabled pairs visible to this MCP scope | *(none)* | Respects `--scope`, `--allow-pairs`, and `--adapter-filter`. |
| `gemini_list_chats` | List `Gemini.app` sidebar conversation titles via daemon control queue | *(none)* | Marks each title as bound/unbound and whether it is visible to this scope; **never exposes `pairId` of pairs outside this scope**. |
| `gemini_pair_init` | Create a new `Gemini.app` Task conversation and bind a `local` pair via daemon control queue | `title` *(required)*, `pair_id`, `label`, `workspace`, `idempotency_key`, `timeout_seconds` (`<= 300`, default `150`) | Always creates `adapter=local` with `ownerScope` set to this scope (`"default"` if `--scope` unset). When omitted, `pair_id` is auto-generated (`<scope>-<ascii-slug-or-6hex>`) and `idempotency_key` defaults to first 32 hex chars of `sha256(scope\|title)`. Max 10 enabled pairs per scope (`pair_limit_reached`). Wait timeout returns `completed: false` + `requestId` — repeat the same call to resume checking. |
| `gemini_send` | Submit a task to a visible pair | `pair_id` *(required)*, `message` *(required)*, `wait` (default `false`), `timeout_seconds` (`1..300`), `return_on_attention`, `idempotency_key` | Non-blocking by default, returning durable `taskId`. Bounded wait returns `isError: false, completed: false` on chunk expiry or `needs_attention`. |
| `gemini_wait` | Wait in bounded chunks for a scoped `taskId` | `task_id` *(required)*, `timeout_seconds` (`1..300`), `return_on_attention` | Re-entrant; respects per-request `notifications/cancelled` without disrupting the session or peer waits. |
| `gemini_read` | Read verified reply payload and receipt for a completed scoped `taskId` | `task_id` *(required)* | Verifies `pairUid` continuity and SHA-256 payload integrity (`sent/payloads/<taskId>.json`, or `sent/<taskId>.json` for `t3` when allowed). |

---

## 3. Host-Native Configurations

> Your AI agent can run these registration commands itself. Afterwards, **open a new session** in the host so the new tools load.

> Replace `<ABS_PATH_TO_BIN>` with the actual resolved directory from `dirname "$(command -v gemini-task-bridge)"` (never write literal unexpanded `$HOME` inside static JSON or TOML fields). Omit `GEMINI_TASK_BRIDGE_ROOT` when using the default root.

### Standard Scoped Setup (Recommended — Enables AI Self-Service `gemini_pair_init`)

#### Claude Code

**CLI registration (shell expands `"$HOME"`):**

```bash
claude mcp add --transport stdio --scope user gemini-task-bridge -- \
  "$HOME/.local/bin/gemini-task-bridge" mcp --scope claude-code
```

**Or project `.mcp.json`:**

```json
{
  "mcpServers": {
    "gemini-task-bridge": {
      "command": "<ABS_PATH_TO_BIN>/gemini-task-bridge",
      "args": ["mcp", "--scope", "claude-code"]
    }
  }
}
```

#### Codex

**CLI registration (shell expands `"$HOME"`):**

```bash
codex mcp add gemini-task-bridge -- \
  "$HOME/.local/bin/gemini-task-bridge" mcp --scope codex
```

**Or native `~/.codex/config.toml`:**

```toml
[mcp_servers.gemini_task_bridge]
command = "<ABS_PATH_TO_BIN>/gemini-task-bridge"
args = ["mcp", "--scope", "codex"]
```

#### Antigravity (CLI / IDE / 2.0)

**`~/.gemini/config/mcp_config.json`:**

```json
{
  "mcpServers": {
    "gemini-task-bridge": {
      "command": "<ABS_PATH_TO_BIN>/gemini-task-bridge",
      "args": ["mcp", "--scope", "antigravity"]
    }
  }
}
```

### Advanced: Least-Privilege Pinning (`--allow-pairs`)

To pin an agent strictly to pre-created pair IDs (without `--scope` auto-visibility of other pairs):

```bash
# Claude Code (pinned to one pre-created local pair)
claude mcp add --transport stdio gemini-task-bridge -- \
  "$HOME/.local/bin/gemini-task-bridge" mcp --adapter-filter local --allow-pairs my_pair

# Codex (pinned to one pre-created local pair)
codex mcp add gemini-task-bridge -- \
  "$HOME/.local/bin/gemini-task-bridge" mcp --adapter-filter local --allow-pairs my_pair
```

Or combine `--scope <host>` with `--allow-pairs shared_pair` so the host can access both its own scope-created pairs and the explicitly shared pair.

**CLI usage & background completion:**
- Where your host explicitly supports background command completion notifications, you may run `gemini-task-bridge send --pair <id> --wait --return-on-attention --json --message "..."` in the background.
- Across all other hosts/versions and MCP workflows, prefer non-blocking `gemini_send(wait=false)` followed by bounded `gemini_wait` / `task wait`.

---

## 4. Two-Stage Acceptance Checklist

### Stage A — Offline Automated Verification (Repo & CI)
1. `python3 -B -m unittest discover -s tests -v` on both **Python 3.9 (zero pip dependencies)** and **Python 3.12**.
2. `bash -n skills/gemini-task-bridge/scripts/install.sh` and CI YAML/JSON/path-hygiene checks.
3. Offline control-plane, error-catalog, doctor-stage, quota (10-pair limit), scope-isolation, crash-recovery, and stdio MCP protocol suites (`2024-11-05`, `2025-03-26`, `2025-06-18`, camelCase aliases, 8-slot wait saturation, instant `ping`, per-request `notifications/cancelled`, `EOF` shutdown, stdout purity).

### Stage B — Clean-User Real-Host Verification (Separate from Static Review)
1. **Unprivileged host control plane**: From a host process **without** macOS Accessibility trust, confirm `gemini_list_chats` and `gemini_pair_init` succeed via the background LaunchAgent.
2. **CLI path**: On an isolated test `--root`, run `gemini-task-bridge doctor`, `pair init --adapter local ...`, `send --wait --return-on-attention`, `task status`, `task wait`, and `task read`.
3. **Real host MCP smoke**: Launch `gemini-task-bridge mcp --scope <host>` from **Claude Code**, **Codex**, and **Antigravity** in a clean user environment and confirm `initialize`, `tools/list`, `gemini_health`, `gemini_pair_init`, and a `gemini_send -> gemini_wait -> gemini_read` roundtrip succeed end-to-end without affecting existing T3 pairs.
