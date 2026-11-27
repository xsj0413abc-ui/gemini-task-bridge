# CLI, Configuration & Rename Compatibility Reference

All commands work via the installed `gemini-task-bridge` wrapper (or the silent backwards-compatible `t3-gemini-bridge` alias), or directly via `python3 -B scripts/gemini_task_bridge.py`.

---

## 1. Environment Variables

Every `GEMINI_TASK_BRIDGE_*` variable falls back to its legacy `T3_GEMINI_BRIDGE_*` counterpart when the new variable is unset.

| Variable | Legacy Compat Variable | Default / Resolution | Purpose |
| --- | --- | --- | --- |
| `GEMINI_TASK_BRIDGE_ROOT` | `T3_GEMINI_BRIDGE_ROOT` | See Data Root Resolution below | Bridge state, queues, control plane, config, pairs, ledger, locks, and logs |
| `GEMINI_TASK_BRIDGE_LABEL` | `T3_GEMINI_BRIDGE_LABEL` | See Label Resolution below | LaunchAgent label and `~/Library/LaunchAgents/<label>.plist` filename |
| `GEMINI_TASK_BRIDGE_LANG` | — | `zh` / `en` (falls back to `LANG`/`LC_ALL` starting with `zh`, else `en`) | Language for human-readable error `message`/`fix` and `doctor` output |
| `GEMINI_TASK_BRIDGE_MCP_SCOPE` | `T3_GEMINI_BRIDGE_MCP_SCOPE` | Unset (or `default` for `gemini_pair_init` owner) | Host scope identifier (`^[a-z0-9][a-z0-9_-]{0,31}$`), e.g. `claude-code`, `codex`, `antigravity` |
| `GEMINI_TASK_BRIDGE_MCP_ALLOW_PAIRS` | `T3_GEMINI_BRIDGE_MCP_ALLOW_PAIRS` | Unset | Comma-separated pair allowlist for `mcp` (empty/invalid fails closed) |
| `GEMINI_TASK_BRIDGE_MCP_ADAPTER_FILTER` | `T3_GEMINI_BRIDGE_MCP_ADAPTER_FILTER` | `local` | Adapter scope for `mcp`: `local`, `t3`, or `all` |
| `GEMINI_TASK_BRIDGE_T3_HOME` | `T3_GEMINI_BRIDGE_T3_HOME` | `~/.t3` | Passed to `t3 thread send --base-dir` and inspected for `userdata/server-runtime.json` (`adapter=t3`) |
| `GEMINI_TASK_BRIDGE_CLI` | `T3_GEMINI_BRIDGE_CLI` | Auto-resolved (`<T3_HOME>/bin/t3`, then `PATH`) | Explicit path to the `t3` CLI binary (`adapter=t3` only) |

Global flag: `--root <dir>` overrides `GEMINI_TASK_BRIDGE_ROOT` for any subcommand.

### Data Root Resolution Order (Zero Automatic Migration)
1. `--root <dir>` CLI flag or `GEMINI_TASK_BRIDGE_ROOT` (compat: `T3_GEMINI_BRIDGE_ROOT`)
2. `~/.local/share/gemini-task-bridge` if it already exists
3. `~/.local/share/t3-gemini-bridge` if it already exists (data is **not** moved)
4. `~/.local/share/gemini-task-bridge` (fresh install default)

### LaunchAgent Label Resolution Order
1. `GEMINI_TASK_BRIDGE_LABEL` (compat: `T3_GEMINI_BRIDGE_LABEL`)
2. Existing LaunchAgent plist label already targeting the resolved data root
3. `io.gemini-task-bridge.daemon`

---

## 2. Rename Compatibility Table (`t3-gemini-bridge` → `gemini-task-bridge`)

| Item | v2.2.0 Name | Backwards Compatibility |
| --- | --- | --- |
| Skill directory | `skills/gemini-task-bridge/` | Replaced via `git mv` in release packaging |
| Skill frontmatter | `name: gemini-task-bridge`, `metadata.version: "2.2.0"` | `doctor` `skillCopies` scans both `gemini-task-bridge` and `t3-gemini-bridge` dirs |
| Main script | `scripts/gemini_task_bridge.py` (`mcp_server.py` imports `gemini_task_bridge`) | `install.sh` also installs an identical `t3_gemini_bridge.py` copy in `$ROOT` so existing LaunchAgents and version checks keep working |
| CLI command | `~/.local/bin/gemini-task-bridge` | `~/.local/bin/t3-gemini-bridge` wrapper installed alongside with identical content and zero stdout output (MCP stdio safe) |
| Environment variables | `GEMINI_TASK_BRIDGE_*` | Falls back to matching `T3_GEMINI_BRIDGE_*` when unset |
| Data directory | `~/.local/share/gemini-task-bridge` | Reuses existing `~/.local/share/t3-gemini-bridge` automatically; never moves data |
| LaunchAgent Label | `io.gemini-task-bridge.daemon` | Reuses existing plist label for the same root; conflicting different labels are blocked (`conflicting_launchagent_for_root`) |
| MCP server name | `gemini-task-bridge` | Existing host entries invoking `t3-gemini-bridge mcp` continue working |
| Plugin manifests | `.claude-plugin/plugin.json`, `marketplace.json` → `gemini-task-bridge` | GitHub redirects old repo URLs after rename |
| Protocol & schemas | `BRIDGE_VERSION = "2.2.0"` | Wire sentinels (`[[T3_BRIDGE_START:` / `[[T3_BRIDGE_DONE:`), `adapter=t3`, and existing `pairs.json`/`ledger.json` fields remain unchanged |

---

## 3. Subcommands

### Diagnostics & Status

```bash
gemini-task-bridge doctor          # Concise human-readable ✅/❌ checklist + exact next step
gemini-task-bridge doctor --json   # Structured JSON with ready, stage, next, daemonHealth, advanced, skillCopies
gemini-task-bridge status [--json] # Includes counts["sent"], counts["sent/payloads"], and all dispatchStates
```

`doctor --json` fields added in v2.2.0 (all previous fields preserved):
- `ready` (`bool`): overall readiness, evaluated strictly from `daemonHealth`.
- `stage` (`str`): first failing stage in order: `runtime` → `daemon` → `daemon_accessibility` → `screen` → `gemini` → `ok`.
- `next` (`str`): one-line plain-language guidance and command/System Settings path.
- `advanced`: includes `currentProcess` (for reference only; does not gate `ready`).
- `skillCopies`: read-only scan of `~/.agents/skills`, `~/.claude/skills`, `$CLAUDE_CONFIG_DIR/skills`, `~/.claude-*/skills`, `~/.codex/skills`, `~/.gemini/skills`, `~/.gemini/antigravity/skills`, `~/.gemini/config/skills` for `t3-gemini-bridge` or `gemini-task-bridge` directories, reporting `path`, `realpath`, `version`, and `stale` (`version != BRIDGE_VERSION`).

### Mode Switching

```bash
gemini-task-bridge mode hybrid      # Respect each pair's `route` field
gemini-task-bridge mode t3-all      # `t3` pairs route straight to T3; new `local` sends fail closed (exit 2)
gemini-task-bridge mode gemini-all  # Set global_mode=hybrid and all enabled pairs route=gemini
```

### 1-to-1 Pair Management (`pair`)

```bash
gemini-task-bridge pair list [--json]

gemini-task-bridge pair gemini-chats \
  [--direct] \
  [--idempotency-key <key>] \
  [--no-wait] \
  [--json]

gemini-task-bridge pair init \
  --id <pair-id> \
  [--adapter t3|local] \
  [--t3-thread <t3-thread-id>] \
  --gemini-title <title> \
  [--reply-to <reply-thread-id>] \
  [--label <display-label>] \
  [--workspace <abs-dir>] \
  [--init-prompt <prompt>] \
  [--reply-mode auto|outbox|ax_capture] \
  [--wait-sec 35.0] \
  [--direct] \
  [--idempotency-key <key>] \
  [--no-wait] \
  [--json]

gemini-task-bridge pair request <requestId> [--wait <seconds>] [--json]

gemini-task-bridge pair add \
  --id <pair-id> \
  [--adapter t3|local] \
  [--t3-thread <t3-thread-id>] \
  --gemini-title <exact-existing-title> \
  [--reply-to <reply-thread-id>] \
  [--label <display-label>] \
  [--workspace <abs-dir>] \
  [--route gemini|t3] \
  [--reply-mode auto|outbox|ax_capture] \
  [--force]

gemini-task-bridge pair route <t3-pair-id> <t3|gemini>
gemini-task-bridge pair remove <pair-id> [--force]
```

Control-plane routing for `pair init` and `pair gemini-chats`:
1. If `.daemon_health.json` is fresh (heartbeat within `3 × poll_interval + 10s`), advertises `"daemon_control_plane"`, and has `axTrusted == true` → submits a control request to the background daemon and waits (default wait: `30s` for `gemini-chats`, `wait_sec + 60s` for `init`).
2. Otherwise, if the current process itself has macOS Accessibility trust → executes in direct AX mode (compatible with environments where the daemon is not installed).
3. Otherwise → fails with `ax_not_trusted`.

New control flags:
- `--direct`: force direct local AX execution (debugging only; requires caller Accessibility trust).
- `--idempotency-key <key>`: client-supplied deduplication key (defaults to a random key on CLI); re-submitting the same key returns the original request's status/result without duplicating UI actions.
- `--no-wait`: enqueue the control request and return `requestId` immediately.
- `pair request <requestId> [--wait N] --json`: poll or wait for an existing control request.

Adapter rules:
- If `--adapter` is omitted, `--t3-thread` is required and defaults to `t3`.
- If `--adapter local` is set, `--t3-thread`, `--reply-to`, and `--route t3` are rejected, and `require_v21_runtime_for_root` must pass.
- `pair init` always mints a fresh `pairUid`; `pair add` mints a fresh `pairUid` whenever `adapter`, `geminiTitle`, `t3ThreadId`, or `defaultReplyThreadId` changes.

### Dispatch (`send`) & Task Lifecycle (`task`)

```bash
gemini-task-bridge send \
  --pair <pair-id> \
  (--message <text> | --message - | --file <path> | --stdin) \
  [--task-id <id>] \
  [--reply-to <whitelisted-thread-id>] \
  [--idempotency-key <key>] \
  [--wait] \
  [--return-on-attention] \
  [--timeout 300] \
  [--json]

gemini-task-bridge task status <taskId> [--json]
gemini-task-bridge task wait   <taskId> [--timeout 300] [--return-on-attention] [--json]
gemini-task-bridge task read   <taskId> [--json]
```

`--message -` is normalized to `--stdin`.

### Stdio MCP Server (`mcp`)

```bash
gemini-task-bridge mcp \
  [--scope <name>] \
  [--allow-pairs id1,id2] \
  [--adapter-filter local|t3|all]
```

- Defaults to `--adapter-filter local`.
- `--scope <name>` must match `^[a-z0-9][a-z0-9_-]{0,31}$` (invalid values fail closed with `invalid_mcp_scope`, exit `2`).
- Empty, blank, empty-segment, or malformed `--allow-pairs` fails closed (exit `2`).

### Daemon & LaunchAgent Management

```bash
gemini-task-bridge once                  # Run a single scan cycle
gemini-task-bridge run                   # Run continuous daemon loop
gemini-task-bridge service status
gemini-task-bridge service install       # Blocks on conflicting LaunchAgent labels for the same root
gemini-task-bridge service restart
gemini-task-bridge service uninstall     # Removes plist only; preserves queue, pairs, and ledger history
```

### Upcoming Commands (v2.3.0 — Not Yet Available)

- `gemini-task-bridge setup` (coming soon): guided install/upgrade, Accessibility picker helper, optional `~/.zshrc` PATH setup, and end-to-end smoke test.
- `gemini-task-bridge connect <claude-code|codex|antigravity> [--check]` (coming soon): automated MCP registration (`--scope`), skill linking, and live `gemini_health` verification.

---

## 4. Error Output Format & Complete CLI Exit Code Table

All CLI errors return structured JSON preserving the machine-readable `error` code while adding localized human-readable `message` and `fix` fields (selected via `GEMINI_TASK_BRIDGE_LANG=zh|en`, then `LANG`/`LC_ALL`, default `en`):

```json
{
  "ok": false,
  "error": "ax_not_trusted",
  "message": "...",
  "fix": "..."
}
```

| Command | `0` | `1` | `2` | `3` |
| --- | --- | --- | --- | --- |
| `pair init` / `pair gemini-chats` / `pair request` | Completed (`ok: true`) or `--no-wait` submitted | — | Validation / `ax_not_trusted` / `pair_limit_reached` / `control_plane_unavailable` / `control_request_unconfirmed` / failed | Control request wait timeout (`{"ok": false, "completed": false, "requestId": ..., "status": "pending"}`; not a failure — resume with `pair request` or same `--idempotency-key`) |
| `send` (async / `direct_t3`) | Queued / forwarded to T3 | — | Validation error / `t3_only` rejecting `local` / gate blocked / delivery failure | — |
| `send --wait` / `task wait` | Terminal success (`sent` with verified `message`, or `direct_t3`) | `wait_timeout` (still active in background) | Validation / gate / ACL / terminal failure (`failed`, `timed_out`, `unknown_timeout`, `not_found`) | `--return-on-attention` hit `needs_attention` |
| `task status` | Status JSON returned (including structured `not_found`) | — | Invalid `taskId` or unreadable ledger | — |
| `task read` | Verified `message` + `receipt` returned | — | Not completed / `direct_t3_has_no_gemini_reply` / `historical_payload_unavailable` / `archived_payload_corrupted` | — |
| `service install` | Installed & bootstrapped | — | `conflicting_launchagent_for_root` or launchctl/config error | — |
| `run` | Clean stop | — | Setup/config error | Daemon `.process.lock` already held |
| `mcp` | Clean EOF shutdown | — | Invalid `--scope` / `--allow-pairs` / `--adapter-filter` or runtime gate failure | — |

---

## 5. Schemas & Default Limits

### `bridge_config.json` (v2)

```json
{
  "schemaVersion": 2,
  "global_mode": "hybrid",
  "poll_interval_seconds": 2.0,
  "notifications": true,
  "ax_capture_grace_seconds": 6.0,
  "max_task_wait_seconds": 1800,
  "max_message_bytes": 32768,
  "max_message_lines": 400,
  "max_receipt_file_bytes": 65536,
  "max_inbox_bytes": 262144
}
```

### `pairs.json` (v2, with optional v2.2 provenance fields)

Older pair records without `ownerScope`, `createdVia`, or `controlRequestId` remain valid.

```json
{
  "schemaVersion": 2,
  "pairs": {
    "claude-code-reviewer": {
      "pairId": "claude-code-reviewer",
      "pairUid": "0123456789abcdef0123456789abcdef",
      "adapter": "local",
      "label": "Code Reviewer",
      "t3ThreadId": null,
      "defaultReplyThreadId": null,
      "allowedReplyThreadIds": [],
      "geminiTitle": "Code Reviewer",
      "workspacePath": "/abs/path/to/workspace",
      "route": "gemini",
      "replyMode": "auto",
      "enabled": true,
      "ownerScope": "claude-code",
      "createdVia": "mcp",
      "controlRequestId": "0123456789abcdef0123456789abcdef",
      "createdAt": "2026-11-27T00:00:00.000+00:00",
      "updatedAt": "2026-11-27T00:00:00.000+00:00"
    }
  }
}
```

### Control Plane Files (`control/`, `0o700` dirs, `0o600` files, ≤ 64 KiB)

- Request (`control/inbox/<requestId>.json`):
  ```json
  {
    "schemaVersion": 1,
    "requestId": "<32 hex>",
    "kind": "list_chats | create_pair",
    "idempotencyKey": "<IDEMPOTENCY_KEY_RE>",
    "source": {"scope": "<scope or null>", "via": "cli | mcp"},
    "createdAt": "2026-11-27T00:00:00.000+00:00",
    "params": {}
  }
  ```
- Idempotency map: `control/keys/<sha256(scope|idempotencyKey)>` → `requestId`.
- Result (`control/done/<requestId>.json`): `{"requestId", "kind", "status": "ok" | "failed" | "unconfirmed", "result" | "error", ...}`. Retained for the most recent **500** entries or **7 days**, pruned automatically by the daemon.

### Identifier Rules
- `scope`: `^[a-z0-9][a-z0-9_-]{0,31}$`
- `pairId`: `^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff_.-]{0,63}$`
- `taskId`: `^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$` (custom IDs ending in `.receipt`, case-insensitive, are rejected on new intake via `reserved_task_id_suffix`)
- `threadId`: `^[A-Za-z0-9][A-Za-z0-9:_.-]{0,255}$`
- `idempotencyKey`: `^[A-Za-z0-9._:-]{1,128}$`

### Archive Layout
- `adapter == "t3"` (normal IDs): `sent/<taskId>.json` + `sent/<taskId>.receipt.json` (`counts["sent"]` increments by `+2`).
- `adapter == "local"` (and legacy `.receipt`-suffixed IDs): `sent/payloads/<taskId>.json` (`counts["sent/payloads"]`) + `sent/<taskId>.receipt.json` (`counts["sent"]`).
