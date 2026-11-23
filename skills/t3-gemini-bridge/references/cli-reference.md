# CLI & Configuration Reference

All commands work via the installed `t3-gemini-bridge` wrapper or directly via `python3 -B scripts/t3_gemini_bridge.py`.

## Environment Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `T3_GEMINI_BRIDGE_ROOT` | `~/.local/share/t3-gemini-bridge` | Bridge state, queues, config, pairs, ledger, locks, and logs |
| `T3_GEMINI_BRIDGE_T3_HOME` | `~/.t3` | Passed to `t3 thread send --base-dir` and inspected for `userdata/server-runtime.json` (for `adapter=t3`) |
| `T3_GEMINI_BRIDGE_CLI` | Auto-resolved (`<T3_HOME>/bin/t3`, then `PATH`) | Explicit path to the `t3` CLI binary |
| `T3_GEMINI_BRIDGE_LABEL` | `io.t3-gemini-bridge.daemon` | LaunchAgent label and `~/Library/LaunchAgents/<label>.plist` filename |
| `T3_GEMINI_BRIDGE_MCP_ALLOW_PAIRS` | Unset (all scoped pairs) | Comma-separated pair allowlist for `mcp` (empty/invalid fails closed) |
| `T3_GEMINI_BRIDGE_MCP_ADAPTER_FILTER` | `local` | Default adapter scope for `mcp`: `local`, `t3`, or `all` |

Global flag: `--root <dir>` overrides `T3_GEMINI_BRIDGE_ROOT` for any subcommand.

---

## Subcommands

### Diagnostics & Status

```bash
t3-gemini-bridge doctor [--json]   # Includes `bridgeVersion` and `t3CliRequired`
t3-gemini-bridge status [--json]   # Includes `counts["sent"]`, `counts["sent/payloads"]`, and all dispatchStates
```

### Mode Switching

```bash
t3-gemini-bridge mode hybrid      # Respect each pair's `route` field
t3-gemini-bridge mode t3-all      # `t3` pairs route straight to T3; new `local` sends fail closed (exit 2)
t3-gemini-bridge mode gemini-all  # Set global_mode=hybrid and all enabled pairs route=gemini
```

### 1-to-1 Pair Management (`pair`)

```bash
t3-gemini-bridge pair list [--json]
t3-gemini-bridge pair gemini-chats [--json]

t3-gemini-bridge pair init \
  --id <pair-id> \
  [--adapter t3|local] \
  [--t3-thread <t3-thread-id>] \
  --gemini-title <title> \
  [--reply-to <reply-thread-id>] \
  [--label <display-label>] \
  [--workspace <abs-dir>] \
  [--init-prompt <prompt>] \
  [--reply-mode auto|outbox|ax_capture] \
  [--wait-sec 35.0]

t3-gemini-bridge pair add \
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

t3-gemini-bridge pair route <t3-pair-id> <t3|gemini>
t3-gemini-bridge pair remove <pair-id> [--force]
```

Adapter rules:
- If `--adapter` is omitted, `--t3-thread` is required and defaults to `t3`.
- If `--adapter local` is set, `--t3-thread`, `--reply-to`, and `--route t3` are rejected, and `require_v21_runtime_for_root` must pass.
- `pair init` always mints a fresh `pairUid`; `pair add` mints a fresh `pairUid` whenever `adapter`, `geminiTitle`, `t3ThreadId`, or `defaultReplyThreadId` changes.

### Dispatch (`send`) & Task Lifecycle (`task`)

```bash
t3-gemini-bridge send \
  --pair <pair-id> \
  (--message <text> | --message - | --file <path> | --stdin) \
  [--task-id <id>] \
  [--reply-to <whitelisted-thread-id>] \
  [--idempotency-key <key>] \
  [--wait] \
  [--return-on-attention] \
  [--timeout 300] \
  [--json]

t3-gemini-bridge task status <taskId> [--json]
t3-gemini-bridge task wait   <taskId> [--timeout 300] [--return-on-attention] [--json]
t3-gemini-bridge task read   <taskId> [--json]
```

`--message -` is normalized to `--stdin`.

### Complete CLI Exit Code Table

| Command | `0` | `1` | `2` | `3` |
| --- | --- | --- | --- | --- |
| `send` (async / `direct_t3`) | Queued / forwarded to T3 | — | Validation error / `t3_only` rejecting `local` / gate blocked / delivery failure | — |
| `send --wait` / `task wait` | Terminal success (`sent` with verified `message`, or `direct_t3`) | `wait_timeout` (still active in background) | Validation / gate / ACL / terminal failure (`failed`, `timed_out`, `unknown_timeout`, `not_found`) | `--return-on-attention` hit `needs_attention` |
| `task status` | Status JSON returned (including structured `not_found`) | — | Invalid `taskId` or unreadable ledger | — |
| `task read` | Verified `message` + `receipt` returned | — | Not completed / `direct_t3_has_no_gemini_reply` / `historical_payload_unavailable` / `archived_payload_corrupted` | — |
| `run` | Clean stop | — | Setup/config error | Daemon `.process.lock` already held |
| `mcp` | Clean EOF shutdown | — | Invalid `--allow-pairs` / `--adapter-filter` or runtime gate failure | — |

### Stdio MCP Server (`mcp`)

```bash
t3-gemini-bridge mcp [--allow-pairs id1,id2] [--adapter-filter local|t3|all]
```

Defaults to `--adapter-filter local`. Empty, blank, empty-segment, or malformed `--allow-pairs` fails closed (exit `2`).

### Daemon & LaunchAgent Management

```bash
t3-gemini-bridge once                  # Run a single scan cycle
t3-gemini-bridge run                   # Run continuous daemon loop
t3-gemini-bridge service status
t3-gemini-bridge service install
t3-gemini-bridge service restart
t3-gemini-bridge service uninstall     # Removes plist only; preserves queue, pairs, and ledger history
```

---

## Schemas & Default Limits

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

### `pairs.json` (v2)

```json
{
  "schemaVersion": 2,
  "pairs": {
    "codex_reviewer": {
      "pairId": "codex_reviewer",
      "pairUid": "0123456789abcdef0123456789abcdef",
      "adapter": "local",
      "label": "Codex Reviewer",
      "t3ThreadId": null,
      "defaultReplyThreadId": null,
      "allowedReplyThreadIds": [],
      "geminiTitle": "Codex Reviewer",
      "workspacePath": "/path/to/workspace",
      "route": "gemini",
      "replyMode": "auto",
      "enabled": true,
      "createdAt": "2026-11-24T00:00:00.000+00:00",
      "updatedAt": "2026-11-24T00:00:00.000+00:00"
    }
  }
}
```

Identifier rules:
- `pairId`: `^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff_.-]{0,63}$`
- `taskId`: `^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$` (custom IDs ending in `.receipt`, case-insensitive, are rejected on new intake via `reserved_task_id_suffix`)
- `threadId`: `^[A-Za-z0-9][A-Za-z0-9:_.-]{0,255}$`
- `idempotencyKey`: `^[A-Za-z0-9._:-]{1,128}$`

Archive layout:
- `adapter == "t3"` (normal IDs): `sent/<taskId>.json` + `sent/<taskId>.receipt.json` (`counts["sent"]` increments by `+2`).
- `adapter == "local"` (and legacy `.receipt`-suffixed IDs): `sent/payloads/<taskId>.json` (`counts["sent/payloads"]`) + `sent/<taskId>.receipt.json` (`counts["sent"]`).
