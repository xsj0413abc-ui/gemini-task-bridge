# t3-gemini-bridge

**Verification status:** Offline bridge and stdio MCP regression tests cover Python 3.9 and 3.12. Real-host end-to-end acceptance for Codex, Claude Code, and Antigravity remains pending (Stage B). Configuration examples are integration instructions, not evidence of a completed host test. See the [acceptance checklist](skills/t3-gemini-bridge/references/mcp-and-multi-agent.md#3-two-stage-acceptance-checklist).

**English** | [简体中文](README.zh-CN.md)

Zero-dependency **1-to-1 automated bridge**, **CLI task runner**, and **stdio MCP server** connecting **T3 Code**, **Claude Code**, **Codex**, and **Antigravity** with Google **Gemini Desktop (`Gemini.app`) Task mode** conversations on macOS.

---

## Why This Exists

- **T3 Code**, **Claude Code**, **Codex**, and **Antigravity** excel at coding, file edits, and agent orchestration, while **Google Gemini Desktop (`Gemini.app`) Task mode** provides native Gemini desktop workflows, local folder context, and separate usage pools.
- Neither side provides an official API to submit prompts directly into a dedicated Gemini Desktop Task conversation, detect inline confirmation cards safely, and return results automatically.
- `t3-gemini-bridge` solves this with macOS native Accessibility (`AXUIElement`), atomic local file queues, a zero-dependency stdio MCP server, and optional `t3 thread send` delivery—with **zero third-party `pip` dependencies** and **zero clipboard/focus hijacking**.

---

## Key Features

- **Universal Multi-Agent Support (`adapter=local` + `adapter=t3`)**:
  - **Claude Code / Codex / Antigravity (`local`)**: No `t3` CLI installation required. Dispatch via scoped stdio MCP (`t3-gemini-bridge mcp`) or CLI (`send` / `task status|wait|read`), with SHA-256 verified archival in `sent/payloads/`.
  - **T3 Code (`t3`)**: Full bidirectional 1-to-1 integration returning replies directly to whitelisted T3 threads via `t3 thread send`, preserving flat `sent/<taskId>.json` + `sent/<taskId>.receipt.json` compatibility and instant `mode t3-all` rollback.
- **Zero-Dependency Stdio MCP Server (`t3-gemini-bridge mcp`)**:
  - Pure Python 3.9+ stdlib JSON-RPC 2.0 supporting MCP `2025-06-18`, `2025-03-26` (batch), and `2024-11-05`.
  - Defaults to `--adapter-filter local` and supports fail-closed `--allow-pairs` isolation so project agents never touch production `t3` pairs.
  - Non-blocking `gemini_send`, bounded chunked `gemini_wait`, 3-lane concurrency (control frames never queue behind waits), and per-request `notifications/cancelled`.
- **Crash-Safe Queues & Single-Link Publishing**:
  - Uses 3-phase hidden staging + `atomic_rename_excl` (`renamex_np` on macOS / `renameat2` on Linux) keeping `st_nlink == 1`, pre-AX `dispatching` intent with Step C re-lock verification, and `28 MiB` / `32 MiB` two-tier ledger capacity protection.
- **Unified `needs_attention` Detection & Bilingual Support**:
  - Detects both English and Simplified Chinese (`zh_CN`) `Needs attention` / `需要注意` sidebar badges and inline confirmation cards (`ActionCardRemyConfirmationCard`, etc.), pausing capture/dispatch without auto-approving.
- **macOS TCC `Python.app` Hardening**:
  - Binds LaunchAgent execution to a dedicated single-hardlink `Python.app` binary so macOS `tccd` never misattributes Accessibility trust to `/usr/bin/git`.

---

## Architecture

```text
T3 Code / Claude Code / Codex / Antigravity
  │
  ├──(CLI: send / task)──▶ 3-phase staging + atomic_rename_excl ──▶ inbox/*.task.json
  └──(MCP: stdio)──────┘                                                   │
                                                                           ▼
`t3 thread send` (t3) /                                   LaunchAgent Daemon (Python 3.9+ stdlib)
`sent/payloads/` (local) ◀── outbox/<taskId>.json ◀────── AXValue + AXPress (silent, Step C re-lock)
                             (or AX reply capture)        Gemini.app (Task Mode)
```

---

## Installation

### 1. Install the Agent Skill

```bash
npx skills add xsj0413abc-ui/t3-gemini-bridge
```

Or via Claude Code Plugin Marketplace:

```text
/plugin marketplace add xsj0413abc-ui/t3-gemini-bridge
/plugin install t3-gemini-bridge@t3-gemini-bridge
```

### 2. Install the macOS Bridge CLI, MCP Server & LaunchAgent

Requirements: **macOS**, **Python 3.9+**, and **Google Gemini Desktop** (`/Applications/Gemini.app`). (`t3` CLI is optional and only required if you use `adapter=t3` pairs.)

```bash
git clone https://github.com/xsj0413abc-ui/t3-gemini-bridge.git
cd t3-gemini-bridge
bash skills/t3-gemini-bridge/scripts/install.sh
```

`install.sh` prints the resolved single-link `TCC Python` path (`Python.app`):

1. Enable that `Python.app` in **System Settings -> Privacy & Security -> Accessibility**.
2. Enable it in **Full Disk Access** if paired workspaces live in `~/Desktop`, `~/Documents`, or `~/Downloads`.
3. Install and verify the background LaunchAgent:

```bash
t3-gemini-bridge service install
t3-gemini-bridge doctor --json
```

---

## Quickstart

### Option 1: Codex / Claude Code / Antigravity (`adapter=local` + MCP or CLI)

```bash
# 1. Create a dedicated Gemini Task chat (no T3 thread needed)
t3-gemini-bridge pair init \
  --id "reviewer" \
  --adapter local \
  --label "Code Reviewer" \
  --gemini-title "Code Reviewer" \
  --workspace "/abs/path/to/project"

# 2. Register the scoped MCP server in Codex or Claude Code
codex mcp add gemini-bridge -- "$HOME/.local/bin/t3-gemini-bridge" mcp --adapter-filter local --allow-pairs reviewer
# or:
claude mcp add --transport stdio gemini-bridge -- "$HOME/.local/bin/t3-gemini-bridge" mcp --adapter-filter local --allow-pairs reviewer

# 3. Or run via CLI task lifecycle
t3-gemini-bridge send --pair reviewer --message "Review /abs/path/to/spec.md"
t3-gemini-bridge task wait <taskId> --return-on-attention --json
t3-gemini-bridge task read <taskId> --json
```

See [skills/t3-gemini-bridge/references/mcp-and-multi-agent.md](skills/t3-gemini-bridge/references/mcp-and-multi-agent.md) for host-native TOML/JSON configuration templates.

### Option 2: T3 Code 1-to-1 Bidirectional Pair (`adapter=t3`)

```bash
t3-gemini-bridge pair init \
  --id "t3_reviewer" \
  --t3-thread "<target-t3-thread-id>" \
  --reply-to "<orchestrator-t3-thread-id>" \
  --gemini-title "T3 Code Reviewer" \
  --workspace "/abs/path/to/project"

t3-gemini-bridge send --pair t3_reviewer --message "Inspect /abs/path/to/spec.md"
t3-gemini-bridge mode t3-all   # Instant rollback to native T3 threads
```

---

## Repository Structure

```text
t3-gemini-bridge/
├── .claude-plugin/                  # Claude Code plugin & marketplace manifests
├── .github/workflows/ci.yml         # Python 3.12 & pure-stdlib Python 3.9 CI
├── skills/t3-gemini-bridge/
│   ├── SKILL.md                     # Agent Skill instructions
│   ├── agents/openai.yaml           # UI metadata & invocation policy
│   ├── references/                  # CLI, MCP/multi-agent, TCC & cutover/safety docs
│   └── scripts/
│       ├── bridge_ax.py             # macOS Accessibility & DiagnosticLogWatcher driver
│       ├── mcp_server.py            # Zero-dependency stdlib stdio MCP server
│       ├── t3_gemini_bridge.py      # Bridge CLI, adapters, queues & LaunchAgent manager
│       └── install.sh               # Atomic non-destructive local installer
└── tests/
    ├── test_bridge.py               # Offline bridge, cutover, crash & ACL unit tests
    └── test_mcp.py                  # Stdio MCP protocol & concurrency tests
```

---

## Running Tests

```bash
python3 -B -m unittest discover -s tests -v
```

---

## Security & Privacy Design

- Runtime directories are created with `0o700` and state files with `0o600` under `umask 0o077`.
- Every file read rejects symlinks (`O_NOFOLLOW` + `no_symlink()`), enforces byte/line caps, verifies `st_nlink == 1`, and rejects files modified mid-read.
- Outbound receipts are stage-gated and cross-checked against frozen intake snapshots (`pairId`, `pairUid`, `adapter`, `replyThreadId`).
- MCP defaults to `local` scope with fail-closed `--allow-pairs`, per-request authorization, and zero global sidebar/count leakage.

---

## License

[MIT](LICENSE) © 2026 xsj0413abc-ui
