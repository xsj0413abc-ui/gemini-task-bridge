# CLI & Configuration Reference

All commands work via the installed `t3-gemini-bridge` wrapper or directly via `python3 -B scripts/t3_gemini_bridge.py`.

## Environment Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `T3_GEMINI_BRIDGE_ROOT` | `~/.local/share/t3-gemini-bridge` | Bridge state, queues, config, pairs, ledger, and logs |
| `T3_GEMINI_BRIDGE_T3_HOME` | `~/.t3` | Passed to `t3 thread send --base-dir` and inspected for `userdata/server-runtime.json` |
| `T3_GEMINI_BRIDGE_CLI` | Auto-resolved (`<T3_HOME>/bin/t3`, then `PATH`) | Explicit path to the `t3` CLI binary |
| `T3_GEMINI_BRIDGE_LABEL` | `io.t3-gemini-bridge.daemon` | LaunchAgent label and `~/Library/LaunchAgents/<label>.plist` filename |

Global flag: `--root <dir>` overrides `T3_GEMINI_BRIDGE_ROOT` for any subcommand.

---

## Subcommands

### Diagnostics & Status

```bash
t3-gemini-bridge doctor [--json]
t3-gemini-bridge status [--json]
```

### Mode Switching

```bash
t3-gemini-bridge mode hybrid      # Respect each pair's `route` field
t3-gemini-bridge mode t3-all      # Route all paired `send` calls straight to pair.t3ThreadId
t3-gemini-bridge mode gemini-all  # Set global_mode=hybrid and all enabled pairs route=gemini
```

### 1-to-1 Pair Management (`pair`)

```bash
t3-gemini-bridge pair list [--json]
t3-gemini-bridge pair gemini-chats [--json]

t3-gemini-bridge pair init \
  --id <pair-id> \
  --t3-thread <t3-thread-id> \
  --gemini-title <title> \
  [--reply-to <reply-thread-id>] \
  [--label <display-label>] \
  [--workspace <abs-dir>] \
  [--init-prompt <prompt>] \
  [--reply-mode auto|outbox|ax_capture] \
  [--wait-sec 35.0]

t3-gemini-bridge pair add \
  --id <pair-id> \
  --t3-thread <t3-thread-id> \
  --gemini-title <exact-existing-title> \
  [--reply-to <reply-thread-id>] \
  [--label <display-label>] \
  [--workspace <abs-dir>] \
  [--route gemini|t3] \
  [--reply-mode auto|outbox|ax_capture]

t3-gemini-bridge pair route <pair-id> <t3|gemini>
t3-gemini-bridge pair remove <pair-id> [--force]
```

### Dispatch (`send`)

```bash
t3-gemini-bridge send \
  --pair <pair-id> \
  (--message <text> | --message - | --file <path> | --stdin) \
  [--task-id <id>] \
  [--reply-to <whitelisted-thread-id>] \
  [--wait] \
  [--timeout 300] \
  [--json]
```

`--message -` is normalized to `--stdin`.

`send` exit codes:
- `0`: Queued (async), delivered directly to T3 (`t3` route), or roundtrip receipt delivered (`--wait`).
- `1`: `--wait` timed out before final receipt (`status: "wait_timeout"`). The task remains tracked by the daemon.
- `2`: Input validation error, target delivery failure, or `unknown_timeout`.

### Daemon & LaunchAgent Management

```bash
t3-gemini-bridge once                  # Run a single scan cycle
t3-gemini-bridge run                   # Run continuous daemon loop (acquires .process.lock; exit 3 if already running)
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
    "reviewer": {
      "pairId": "reviewer",
      "label": "Code Reviewer",
      "t3ThreadId": "thread-uuid-or-id",
      "defaultReplyThreadId": "orchestrator-thread-id",
      "allowedReplyThreadIds": ["orchestrator-thread-id", "thread-uuid-or-id"],
      "geminiTitle": "Code Reviewer",
      "workspacePath": "/path/to/workspace",
      "route": "gemini",
      "replyMode": "auto",
      "enabled": true,
      "createdAt": "2026-11-21T00:00:00.000+00:00",
      "updatedAt": "2026-11-21T00:00:00.000+00:00"
    }
  }
}
```

Identifier rules:
- `pairId`: `^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff_.-]{0,63}$`
- `taskId`: `^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`
- `threadId`: `^[A-Za-z0-9][A-Za-z0-9:_.-]{0,255}$`

### Structured Outbox Receipt (`outbox/<taskId>.json`)

```json
{
  "taskId": "20261121-120000-abcdef",
  "pairId": "reviewer",
  "message": "Concise findings and /abs/path/to/artifacts"
}
```

Note: The filename stem `<taskId>` MUST strictly match `data["taskId"]`, and `replyThreadId` must belong to the pair's whitelist (`t3ThreadId`, `defaultReplyThreadId`, `allowedReplyThreadIds`, or another enabled pair's `t3ThreadId`).
