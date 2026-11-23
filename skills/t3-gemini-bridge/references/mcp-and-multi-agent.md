# Multi-Agent & Stdio MCP Guide (Codex / Claude Code / Antigravity)

`t3-gemini-bridge mcp` provides a zero-dependency Python 3.9+ stdlib MCP stdio server (`2025-06-18`, `2025-03-26`, and `2024-11-05`) so **Codex**, **Claude Code**, and **Antigravity** can drive `Gemini.app` Task mode without installing T3 Code or risking interference with existing T3 production pairs.

---

## 1. Security Model & Scope Defaults

- **Default adapter filter**: `--adapter-filter local` (T3 pairs are hidden by default; set `--adapter-filter all` or `t3` only when explicitly intended).
- **Explicit pair allowlist**: Pass `--allow-pairs pair_a,pair_b` (or `T3_GEMINI_BRIDGE_MCP_ALLOW_PAIRS`). Any empty string, whitespace, empty segment (`"a,"`), or invalid identifier **fails closed at startup** (`exit 2`).
- **Minimum-privilege tool surface**:
  - `gemini_health`: system liveness + scoped counts/attention list only (no global pair/count leakage).
  - `gemini_pairs`: scoped enabled pairs only (no full Gemini sidebar enumeration).
  - `gemini_send`: defaults to `wait=false` (returns durable `taskId` immediately; supports `idempotency_key`). Bounded wait (`wait=true`, `1..300s`) returns `isError: false, completed: false` on chunk expiry or `needs_attention` so clients never lose `taskId`.
  - `gemini_wait`: chunked re-entrant waiting (`1..300s`), respects `notifications/cancelled` per request without tearing down the session or peer waits.
  - `gemini_read`: reads verified `sent/payloads/<taskId>.json` (or flat `sent/<taskId>.json` for `t3` when allowed) after verifying `pairUid` continuity.
- **3-Lane Concurrency**:
  - Control frames (`initialize`, `notifications/initialized`, `notifications/cancelled`, `ping`, `tools/list`) run directly on the reader thread with zero queue delay.
  - Non-blocking tools (`gemini_health`, `gemini_pairs`, `gemini_read`, `gemini_send(wait=false)`) run in a bounded fast pool (overflow returns `mcp_server_busy`).
  - Blocking waits (`gemini_wait`, `gemini_send(wait=true)`, `direct_t3`) are capped by a non-blocking semaphore (`max_concurrent_waits = 8`; a 9th concurrent wait fails fast with `too_many_concurrent_waits`).
- **OS Boundary Note**: Scope isolation enforces access boundaries for this MCP server protocol interface; it is not OS-level filesystem sandboxing between processes running under the same macOS user account.

---

## 2. Host-Native Configurations

> Replace `<ABSOLUTE_PATH_TO_BIN>` with the actual resolved path from `command -v t3-gemini-bridge` (e.g., `/Users/<you>/.local/bin` on your local machine). Never write literal unexpanded `$HOME` inside static JSON or TOML fields, and omit `T3_GEMINI_BRIDGE_ROOT` when using the default root.

### Codex

**CLI registration (shell expands `"$HOME"`):**

```bash
codex mcp add gemini-bridge -- "$HOME/.local/bin/t3-gemini-bridge" mcp --adapter-filter local --allow-pairs my_pair
```

**Or native `~/.codex/config.toml`:**

```toml
[mcp_servers.gemini_bridge]
command = "<ABSOLUTE_PATH_TO_BIN>/t3-gemini-bridge"
args = ["mcp", "--adapter-filter", "local", "--allow-pairs", "my_pair"]
```

### Claude Code

**CLI registration (shell expands `"$HOME"`):**

```bash
claude mcp add --transport stdio gemini-bridge -- "$HOME/.local/bin/t3-gemini-bridge" mcp --adapter-filter local --allow-pairs my_pair
```

**Or project `.mcp.json`:**

```json
{
  "mcpServers": {
    "gemini-bridge": {
      "command": "<ABSOLUTE_PATH_TO_BIN>/t3-gemini-bridge",
      "args": ["mcp", "--adapter-filter", "local", "--allow-pairs", "my_pair"]
    }
  }
}
```

### Antigravity (CLI / IDE / 2.0)

**MCP server config:**

```json
{
  "mcpServers": {
    "gemini-bridge": {
      "command": "<ABSOLUTE_PATH_TO_BIN>/t3-gemini-bridge",
      "args": ["mcp", "--adapter-filter", "local", "--allow-pairs", "my_pair"]
    }
  }
}
```

**CLI usage & background completion:**
- Where your host version explicitly supports background command completion notifications (such as verified Antigravity / Claude Code background terminal commands), you may run `t3-gemini-bridge send --pair <id> --wait --return-on-attention --json --message "..."` in the background.
- Across all other hosts/versions and MCP workflows, always use non-blocking `gemini_send(wait=false)` (or async CLI `send`) to obtain the durable `taskId`, followed by bounded `gemini_wait` / `task wait`.

---

## 3. Two-Stage Acceptance Checklist

### Stage A — Offline Automated Verification (Repo & CI)
1. `python3 -B -m unittest discover -s tests -v` on both **Python 3.9 (zero pip dependencies)** and **Python 3.12**.
2. `bash -n skills/t3-gemini-bridge/scripts/install.sh` and CI YAML/JSON/path-hygiene checks.
3. Subprocess stdio MCP suite verifying `2024-11-05`, `2025-03-26` (batch), `2025-06-18` (reject batch), 8-slot wait saturation, instant `ping`, single-request `notifications/cancelled`, `EOF` shutdown, and stdout purity.

### Stage B — Clean-User Real-Host Verification (Separate from Static Review)
1. **CLI path**: On an isolated test `--root`, run `t3-gemini-bridge pair init --adapter local ...`, `send --wait --return-on-attention`, `task status`, `task wait`, and `task read`.
2. **Real host MCP smoke**: Launch the installed `t3-gemini-bridge mcp ...` from each target host (**Codex**, **Claude Code**, **Antigravity**) in a clean user environment and confirm `initialize`, `tools/list`, `gemini_health`, and a `gemini_send -> gemini_wait -> gemini_read` roundtrip succeed end-to-end.
