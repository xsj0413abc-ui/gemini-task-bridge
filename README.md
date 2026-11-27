# gemini-task-bridge

**English** | [简体中文](README.zh-CN.md)

Bridge **Claude Code**, **Codex**, **Antigravity**, and **T3 Code** directly to **Google Gemini Desktop (`Gemini.app`) Task mode** via a local stdio MCP server and CLI—so your coding AI can create dedicated Gemini conversations, send tasks, and read verified replies automatically. **macOS only** (requires Python 3.9+ with zero third-party dependencies and `Gemini.app`). **Unofficial community tool**—not affiliated with, endorsed by, or supported by Google.

> **Verification status:** Offline unit tests and stdio MCP regression suites cover Python 3.9 (pure stdlib) and Python 3.12. Real-host end-to-end acceptance across Codex, Claude Code, and Antigravity is tracked in [mcp-and-multi-agent.md](skills/gemini-task-bridge/references/mcp-and-multi-agent.md#4-two-stage-acceptance-checklist).

---

## 3-Minute Quickstart (macOS)

Only the background LaunchAgent needs macOS Accessibility permission—**Claude Code, Codex, Antigravity, and your terminal do not**.

> **Easiest path:** install the skill, then ask your AI agent (Claude Code, Codex, or Antigravity) to "install and set up gemini-task-bridge". It follows `SKILL.md` and runs every step below for you; you only toggle `Python.app` on in System Settings → Accessibility.

### 1. Install the CLI and background service

```bash
git clone https://github.com/xsj0413abc-ui/gemini-task-bridge.git
cd gemini-task-bridge
bash skills/gemini-task-bridge/scripts/install.sh --install-service --add-to-path
```

If needed, the installer backs up your existing `~/.zshrc` and adds `~/.local/bin` to the PATH for new zsh shells.

### 2. Enable Accessibility and check readiness

In **System Settings → Privacy & Security → Accessibility**, enable the `Python.app` selected in Finder (add it if it is not listed). If the installer didn't open Settings, use the printed TCC Python path to locate the app.

Run `"$HOME/.local/bin/gemini-task-bridge" doctor --json` every few seconds until `ready: true`. Follow `next` if blocked, for example if the service needs a restart or `Gemini.app` isn't running.

### 3. Register MCP in your AI host

- **Claude Code** (default config)
  ```bash
  env -u CLAUDE_CONFIG_DIR claude mcp add --transport stdio --scope user gemini-task-bridge -- \
    "$HOME/.local/bin/gemini-task-bridge" mcp --scope claude-code
  ```
  Repeat for each custom config directory you use, replacing `env -u CLAUDE_CONFIG_DIR` with `CLAUDE_CONFIG_DIR="/absolute/path/to/config"`.
- **Codex**
  ```bash
  codex mcp add gemini-task-bridge -- \
    "$HOME/.local/bin/gemini-task-bridge" mcp --scope codex
  ```
- **Antigravity** — back up `~/.gemini/config/mcp_config.json` if it exists. Treat a missing or empty file as `{}` and merge only this entry into `mcpServers`. Keep all other entries and keys and use the actual absolute path, not a literal `$HOME`.
  ```json
  {
    "gemini-task-bridge": {
      "command": "<ABS_PATH_TO_BIN>/gemini-task-bridge",
      "args": ["mcp", "--scope", "antigravity"]
    }
  }
  ```

Optional — install the Agent Skill so your AI knows the full diagnostic and dispatch workflow:

```bash
npx skills add xsj0413abc-ui/gemini-task-bridge
```

Or in Claude Code Plugin Marketplace:

```text
/plugin marketplace add xsj0413abc-ui/gemini-task-bridge
/plugin install gemini-task-bridge@gemini-task-bridge
```

### 4. Open a new session and verify a reply

Start a new session in your AI host. Ask your AI to create a uniquely named test conversation with `gemini_pair_init`, ask Gemini to echo a random code with `gemini_send`, then use `gemini_wait` to check that the reply contains the exact code.

---

## Key Features (v2.2.0)

- **AI Self-Service Conversation Creation (Background Control Plane)**:
  - `gemini_pair_init` and `gemini_list_chats` (plus CLI `pair init` / `pair gemini-chats`) submit requests to the privileged LaunchAgent over an atomic control queue—so AI hosts never need macOS Accessibility permission.
  - Per-scope quota of **10 enabled pairs** prevents runaway chat creation, with idempotent request keys and crash-safe startup recovery.
- **Human-Readable Bilingual Errors & Stage-by-Stage `doctor`**:
  - Every CLI and MCP error includes a plain-language `message` and actionable `fix` in Chinese or English (controlled by `GEMINI_TASK_BRIDGE_LANG=zh|en`, falling back to `LANG`/`LC_ALL`, default `en`).
  - `gemini-task-bridge doctor` evaluates background daemon health (`daemon → daemon_accessibility → screen → gemini → ok`).
- **Multi-Host Scoped Stdio MCP Server (`gemini-task-bridge mcp`)**:
  - Zero-dependency Python 3.9+ stdlib JSON-RPC 2.0 supporting MCP `2025-06-18`, `2025-03-26` (batch), and `2024-11-05`.
  - `--scope <name>` isolates pairs and tasks per host; accepts both snake_case and camelCase IDs (`pair_id`/`pairId`, `task_id`/`taskId`, `idempotency_key`/`idempotencyKey`); defaults to `--adapter-filter local` so T3 production pairs stay untouched.
- **Crash-Safe Queues & Zero Focus Hijacking**:
  - 3-phase hidden staging + `atomic_rename_excl` (`st_nlink == 1`), pre-AX `dispatching` intent with Step C re-lock verification, and silent `AXValue`/`AXPress` injection without stealing clipboard or focus.
- **Bilingual `needs_attention` Detection & TCC Hardening**:
  - Detects English and Simplified Chinese `Needs attention` / `需要注意` badges and confirmation cards without auto-approving.
  - Binds LaunchAgent to a single-hardlink `Python.app` binary to prevent macOS `tccd` misattribution to `/usr/bin/git`, and guards against conflicting LaunchAgents targeting the same data root.

---

## T3 Code Integration (`adapter=t3`)

T3 Code users get full 1-to-1 bidirectional thread delivery via the official `t3 thread send` CLI, preserving flat `sent/<taskId>.json` + `sent/<taskId>.receipt.json` archives and instant rollback to native T3 threads. (`t3` CLI is only required when using `adapter=t3` pairs.)

```bash
# Create a 1-to-1 T3 Code <-> Gemini Task pair
gemini-task-bridge pair init \
  --id "t3_reviewer" \
  --adapter t3 \
  --t3-thread "<target-t3-thread-id>" \
  --reply-to "<orchestrator-t3-thread-id>" \
  --gemini-title "T3 Code Reviewer" \
  --workspace "/abs/path/to/project"

# Dispatch a task (replies return to --reply-to automatically)
gemini-task-bridge send --pair t3_reviewer --message "Inspect /abs/path/to/spec.md"

# Emergency rollback / per-pair routing
gemini-task-bridge mode t3-all               # Route all t3 pairs back to T3; pause local pairs
gemini-task-bridge mode hybrid               # Restore per-pair routing
gemini-task-bridge pair route t3_reviewer t3 # Switch a single t3 pair
```

---

## Advanced: Least-Privilege Isolation (`--allow-pairs`)

By default, `gemini-task-bridge mcp --scope <host>` lets that host see pairs it created (`ownerScope == scope`). If you want to restrict an agent to specific pre-created pairs—or grant a scoped host read/write access to a shared pair—add `--allow-pairs`:

```bash
# Restrict a project agent strictly to one pre-created local pair (no --scope)
gemini-task-bridge mcp --adapter-filter local --allow-pairs reviewer

# Or grant a scoped host access to its own pairs PLUS a shared pair
gemini-task-bridge mcp --scope claude-code --allow-pairs shared_reviewer
```

Empty, blank, or malformed `--allow-pairs` values fail closed at startup (`exit 2`). See [mcp-and-multi-agent.md](skills/gemini-task-bridge/references/mcp-and-multi-agent.md) for host-native TOML/JSON templates and full tool schemas.

---

## Upgrading from `t3-gemini-bridge`

v2.2.0 renames the project from `t3-gemini-bridge` to `gemini-task-bridge` with **zero-migration backwards compatibility**:

- **CLI**: `~/.local/bin/gemini-task-bridge` is installed alongside a silent `t3-gemini-bridge` compatibility wrapper.
- **Data root**: Automatically continues using `~/.local/share/t3-gemini-bridge` if it already exists and `~/.local/share/gemini-task-bridge` does not (no data is moved).
- **Environment variables & LaunchAgent**: All `GEMINI_TASK_BRIDGE_*` variables fall back to `T3_GEMINI_BRIDGE_*`, and existing LaunchAgent labels targeting the same root are preserved (conflicting labels are blocked safely with `conflicting_launchagent_for_root`).

See [cli-reference.md](skills/gemini-task-bridge/references/cli-reference.md) for the full compatibility matrix and [macos-permissions-and-troubleshooting.md](skills/gemini-task-bridge/references/macos-permissions-and-troubleshooting.md) for the `v2.0` production cutover SOP.

---

## Architecture

```text
Claude Code / Codex / Antigravity / T3 Code
  │
  ├──(MCP stdio / CLI: pair init, list_chats)──▶ control/inbox/*.json ─────────┐
  └──(MCP stdio / CLI: send, task wait|read)───▶ inbox/*.task.json ────────────┤
                                                                               ▼
`sent/payloads/` (local) /                                    LaunchAgent Daemon (Python.app)
`t3 thread send` (t3)     ◀── outbox/<taskId>.json ◀───────── AXValue + AXPress (silent)
                              (or AX reply capture)           Gemini.app (Task Mode)
```

---

## Repository Structure

```text
gemini-task-bridge/
├── .claude-plugin/                         # Claude Code plugin & marketplace manifests
├── .github/workflows/ci.yml                # Python 3.12 & pure-stdlib Python 3.9 CI
├── skills/gemini-task-bridge/
│   ├── SKILL.md                            # Agent Skill operating manual
│   ├── agents/openai.yaml                  # UI metadata & invocation policy
│   ├── references/                         # CLI, MCP, macOS TCC/troubleshooting & safety docs
│   └── scripts/
│       ├── gemini_task_bridge.py           # Bridge CLI, adapters, queues & LaunchAgent manager
│       ├── control_plane.py                # Daemon control queue, quotas & crash recovery
│       ├── error_catalog.py                # Bilingual human-readable error messages & fixes
│       ├── bridge_ax.py                    # macOS Accessibility & DiagnosticLogWatcher driver
│       ├── mcp_server.py                   # Zero-dependency stdlib stdio MCP server
│       └── install.sh                      # Atomic non-destructive local installer
└── tests/                                  # Offline unit & stdio MCP regression tests
```

---

## Running Tests

```bash
python3 -B -m unittest discover -s tests -v
```

---

## Security & Privacy Design

- Runtime directories are created with `0o700` and state/control files with `0o600` under `umask 0o077`.
- File reads reject symlinks (`O_NOFOLLOW` + `no_symlink()`), verify `st_nlink == 1` and `st_uid == os.getuid()`, and enforce strict byte/line limits.
- Control requests are fully re-validated on the daemon side; `in_flight` creation crashes never blindly re-click `Gemini.app`.
- Outbound receipts are stage-gated and cross-checked against frozen intake snapshots (`pairId`, `pairUid`, `adapter`, `replyThreadId`).

---

## License

[MIT](LICENSE) © 2026 xsj0413abc-ui
