# t3-gemini-bridge

**English** | [简体中文](README.zh-CN.md)

Zero-dependency **1-to-1 automated bidirectional bridge** and **Agent Skill** connecting [T3 Code](https://github.com/pingdotgg/t3code) threads with Google **Gemini Desktop (`Gemini.app`) Task mode** conversations on macOS.

---

## Why This Exists

- **T3 Code** excels at multi-thread agent orchestration (Claude Code, Codex, Antigravity, etc.), while **Google Gemini Desktop (`Gemini.app`) Task mode** offers native Gemini desktop capabilities, local folder context, and separate usage pools.
- Neither tool provides an official API to submit prompts directly into an existing Gemini Desktop Task conversation and automatically route results back into a specific T3 Code thread.
- `t3-gemini-bridge` solves this cleanly using macOS native Accessibility (`AXUIElement`) + the official `t3` CLI—with **zero third-party `pip` dependencies**, **zero clipboard/focus hijacking**, and **instant 1-command rollback to T3-only**.

---

## Key Features

- **Fully Automated Roundtrip (No Manual Wake-Up)**:
  - **Outbound**: `t3-gemini-bridge send` atomically queues tasks in `inbox/`. A macOS LaunchAgent daemon silently switches `Gemini.app` to the paired conversation, injects the prompt via `AXValue`, and submits via `AXPress`—without touching your clipboard or stealing window focus.
  - **Inbound**: Supports both structured JSON receipts (`outbox/<taskId>.json`) and automatic AX reply extraction fallback, delivering clean results back into the target T3 thread via `t3 thread send`.
- **Strict 1-to-1 Isolation & 4-Layer Anti-Crosstalk**:
  - `pairs.json` forbids two enabled pairs from sharing the same `t3ThreadId` or `geminiTitle`.
  - Verifies `AXWindow.AXTitle == geminiTitle` before every injection/capture, skips busy/confirmation states (`in_progress`, `needs_attention`, stop button), and serializes dispatches per pair FIFO while running distinct pairs concurrently.
- **Split-Sentinel Prompt-Echo Immunity**:
  - Wraps dispatches with `[[T3_BRIDGE_START:<taskId>]]` and split completion instructions so prompt echoes in the AX tree never trigger premature capture, while cleanly isolating new replies across multi-turn conversations.
- **Instant Rollback to T3-Only (`mode t3-all`)**:
  - Switch all pairs back to native T3 threads instantly with `t3-gemini-bridge mode t3-all` (or per pair with `pair route <id> t3`). Existing T3 threads and files remain untouched.
- **macOS TCC `Python.app` Hardening**:
  - Automatically avoids Apple's `/usr/bin/python3` ~78-way hardlink shim (which macOS `tccd` frequently misattributes to `/usr/bin/git`), binding the LaunchAgent to a dedicated single-link `Python.app` binary instead.
- **Bilingual Gemini Desktop UI Support**:
  - Supports both English and Simplified Chinese (`zh_CN`) `Gemini.app` accessibility labels, badges, confirmation cards, and folder pickers.

---

## Architecture

```text
T3 Code Thread ──(send)──▶ inbox/*.task.json ──▶ LaunchAgent Daemon (Python 3.9+ stdlib)
      ▲                                                  │
      │                                       AXValue + AXPress (silent)
      │                                                  ▼
`t3 thread send` ◀── outbox/<taskId>.json ◀──── Gemini.app (Task Mode)
                     (or AX reply capture)
```

- **No token theft or private DB writes**: Never touches T3's `statev2.sqlite`, internal provider tokens, or Gemini's SwiftData store.
- **Fail-closed idempotency**: Marks deliveries `sending` before invoking `t3` and transitions interrupted sends to `unknown_timeout` with zero auto-retries; duplicate receipts go to `dedup/`.

---

## Installation

### 1. Install the Agent Skill

Using Vercel's `skills` CLI (works with T3 Code, Claude Code, Codex, Antigravity, Cursor, etc.):

```bash
npx skills add xsj0413abc-ui/t3-gemini-bridge
```

Or via Claude Code Plugin Marketplace:

```text
/plugin marketplace add xsj0413abc-ui/t3-gemini-bridge
/plugin install t3-gemini-bridge@t3-gemini-bridge
```

### 2. Install the macOS Bridge CLI & LaunchAgent

Requirements: **macOS**, **Python 3.9+**, **T3 Code** (`t3` CLI at `~/.t3/bin/t3` or on `PATH`), and **Google Gemini Desktop** (`/Applications/Gemini.app`).

```bash
git clone https://github.com/xsj0413abc-ui/t3-gemini-bridge.git
cd t3-gemini-bridge
bash skills/t3-gemini-bridge/scripts/install.sh
```

`install.sh` prints the resolved single-link `TCC Python` path (a `Python.app` executable):

1. Open **System Settings -> Privacy & Security -> Accessibility** and enable that `Python.app`.
2. If your paired workspaces live in `~/Desktop`, `~/Documents`, or `~/Downloads`, also enable that `Python.app` in **Full Disk Access**.
3. Install and verify the background LaunchAgent:

```bash
t3-gemini-bridge service install
t3-gemini-bridge doctor --json
```

Confirm these 4 health checks pass:
- `launchAgent.state == "running"`
- `daemonHealth.axTrusted == true`
- `daemonHealth.screenLocked == false`
- `daemonHealth.geminiRunning == true`

---

## Quickstart

### Bind a T3 Thread 1-to-1 with a Gemini Task Conversation

**Create a brand-new Gemini Task chat and attach a workspace:**

> Note: Ensure the target workspace folder has been opened/bookmarked at least once in `Gemini.app`'s **Folders** picker.

```bash
t3-gemini-bridge pair init \
  --id "reviewer" \
  --label "Code Reviewer" \
  --t3-thread "<target-t3-thread-id>" \
  --reply-to "<orchestrator-t3-thread-id>" \
  --gemini-title "Code Reviewer" \
  --workspace "/abs/path/to/project"
```

**Or bind an existing Gemini Desktop chat:**

```bash
t3-gemini-bridge pair gemini-chats --json
t3-gemini-bridge pair add \
  --id "reviewer" \
  --t3-thread "<target-t3-thread-id>" \
  --reply-to "<orchestrator-t3-thread-id>" \
  --gemini-title "<exact-gemini-title>"
```

### Dispatch Tasks & Receive Automatic Replies

```bash
# Async dispatch (returns taskId immediately; reply arrives in --reply-to T3 thread)
t3-gemini-bridge send --pair reviewer --message "Review /abs/path/to/spec.md and list blockers."

# Dispatch from file or stdin
t3-gemini-bridge send --pair reviewer --file /abs/path/to/task.md

# Wait synchronously for roundtrip completion
t3-gemini-bridge send --pair reviewer --wait --timeout 300 --json --message "..."
```

### Switch Instantly Between Gemini Bridge and T3-Only

```bash
# Instant global rollback to native T3 threads (zero Gemini interaction)
t3-gemini-bridge mode t3-all

# Restore hybrid routing
t3-gemini-bridge mode hybrid

# Switch a single pair independently
t3-gemini-bridge pair route reviewer t3
t3-gemini-bridge pair route reviewer gemini
```

---

## Repository Structure

```text
t3-gemini-bridge/
├── .claude-plugin/                  # Claude Code plugin & marketplace manifests
├── .github/workflows/ci.yml         # Cross-OS unit tests, skill & hygiene checks
├── skills/t3-gemini-bridge/
│   ├── SKILL.md                     # Agent Skill instructions (progressive disclosure)
│   ├── agents/openai.yaml           # UI metadata & invocation policy
│   ├── references/                  # CLI schema, TCC guide, architecture & safety docs
│   └── scripts/
│       ├── bridge_ax.py             # macOS Accessibility & DiagnosticLogWatcher driver
│       ├── t3_gemini_bridge.py      # Bridge CLI, queue state machine & LaunchAgent manager
│       └── install.sh               # Non-destructive local installer
└── tests/
    └── test_bridge.py               # 26 offline unit tests (mocked AX & fake t3 CLI)
```

---

## Running Tests

```bash
python3 -B -m unittest discover -s tests -v
```

---

## Security & Privacy Design

- Runtime directories are created with `0o700` and state files with `0o600` under `umask 0o077`.
- Every file read rejects symlinks (`O_NOFOLLOW` + parent `no_symlink()` walk), enforces byte/line caps, verifies `st_nlink == 1`, and rejects files modified mid-read.
- Outbound `replyThreadId` values are validated against each pair's strict allowlist.
- No hardcoded user paths, tokens, or account identifiers exist in the codebase.

---

## License

[MIT](LICENSE) © 2026 xsj0413abc-ui
