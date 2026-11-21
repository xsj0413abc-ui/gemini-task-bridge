# Architecture, Protocol & Safety Invariants

## 1. System Architecture

```text
+-------------------------------------------------------------------------+
| T3 Code (Orchestrator / User Thread)                                    |
|   Calls: t3-gemini-bridge send --pair <id> --message "..."              |
+------------------------------------+------------------------------------+
                                     |
         +---------------------------+---------------------------+
         | global_mode == "t3_only" OR pair.route == "t3"        | global_mode == "hybrid" AND pair.route == "gemini"
         v                                                       v
+----------------------------------+        +---------------------------------------------+
| Direct T3 Delivery               |        | Atomic File Queue (<root>/inbox/*.task.json)|
|   `t3 thread send --json`        |        +----------------------+----------------------+
+----------------------------------+                               |
                                                                   v
                                            +---------------------------------------------+
                                            | LaunchAgent Daemon (2s poll, Python 3.9+ stdlib)
                                            |   1. Single-link Python.app TCC binary      |
                                            |   2. 4-layer anti-crosstalk guard (.gemini_ui.lock)
                                            |   3. Injects via AXValue + AXPress (no clipboard/focus)
                                            +----------------------+----------------------+
                                                                   |
                                                                   v
                                            +---------------------------------------------+
                                            | Gemini.app (Task Mode Conversation)         |
                                            |   Primary return: outbox/<taskId>.json      |
                                            |   Fallback return: AX reply capture         |
                                            +----------------------+----------------------+
                                                                   |
                                                                   v
                                            +---------------------------------------------+
                                            | Idempotent Receipt Pipeline                 |
                                            |   outbox/ -> sending/ -> `t3 thread send`   |
                                            |   -> sent/<taskId>.receipt.json & ledger    |
                                            |   (duplicates -> dedup/, errors -> failed/) |
                                            +---------------------------------------------+
```

### Why No Token Copying or Database Hacking

- T3 Code's internal MCP bearer tokens (`McpSessionRegistry`) are ephemeral, in-memory, and scoped to individual provider sessions—copying them breaks across restarts and mixes thread identities.
- Both T3 (`statev2.sqlite`) and Gemini (`custom-local-mcp-servers.store`) own private schemas; writing directly to them bypasses projections and scheduling.
- Instead, inbound delivery uses the official `t3 thread send` CLI (short-lived, scoped external session handled by `t3` itself), and outbound dispatch uses macOS Accessibility `AXUIElement` directly on `Gemini.app` without touching the clipboard (`pbcopy`) or stealing window focus (`AXHidden` is preserved).

---

## 2. Four-Layer Anti-Crosstalk Guard

1. **Unique 1-to-1 registry (`pairs.json`)**: `validate_pairs_data()` rejects any configuration where two enabled pairs share the same `t3ThreadId` or `geminiTitle`.
2. **Exact navigation & closed-loop title verification**: Before injecting a prompt (or capturing a reply), `open_conversation()` navigates via sidebar -> menu bar -> search, ignoring floating search popups (`title == ""`) and `AXDialog` / MiniChat windows, and strictly requires `normalize_window_title(AXTitle) == pair["geminiTitle"]`. Any mismatch aborts fail-closed without touching the composer.
3. **Busy & confirmation avoidance**: Dispatches skip any conversation whose sidebar badge is `in_progress` / `needs_attention`, or whose window currently shows a stop button (`stop_button` / `AGENT_WORK_PANE_STOP_BUTTON`), generating indicator, or confirmation card (`ActionCardRemyConfirmationCard`, etc.).
4. **Per-pair FIFO & global UI lock**: `.gemini_ui.lock` serializes brief AX reads/writes across the app, while `_active_in_flight_by_pair()` limits each `pairId` to one `in_flight` task at a time so multi-turn context never interleaves. Different `pairId`s run concurrently.

---

## 3. Split-Sentinel Prompt-Echo Immunity & Reply Slicing

`build_framed_prompt()` wraps every outbound dispatch with:
- Start tag: `[[T3_BRIDGE_START:<taskId>]]`
- Split done instructions: explicitly separates `"[[T3_BRIDGE_DONE:"` and `"<taskId>]]"` in the prompt text so when Gemini echoes or renders the user's prompt bubble in the AX tree, the unbroken done sentinel `[[T3_BRIDGE_DONE:<taskId>]]` **never** exists inside the prompt itself.

When capturing replies from AX (`extract_gemini_reply()`):
1. Slices strictly after the last `[[T3_BRIDGE_START:<taskId>]]` chunk if present.
2. Enforces a floor past any other turn's `[[T3_BRIDGE_DONE:...]]` sentinel.
3. Handles folded top bubbles and non-chronological UI chrome by matching against the `baselineChunks` tail captured immediately prior to dispatch.
4. Strips echoed prompt blocks, sentinels, and bilingual EN/ZH UI chrome (`Gemini said`, `Gemini 说`, `Show thinking`, `显示思路`, `Copy`, `拷贝`, `What's next?`, `接下来做什么？`, etc.), then enforces configured byte/line caps (`max_message_bytes`, `max_message_lines`).

---

## 4. Reliability, Idempotency & Filesystem Security

- **At-most-once T3 delivery (`unknown_timeout` fail-closed)**: Outbox files move `outbox/ -> sending/` under `.ledger.lock` before invoking `t3 thread send`. If `t3` times out (60s) or the daemon crashes mid-send (`recover()` on startup), the task transitions to `unknown_timeout` and is moved to `failed/` with **zero automatic retries**, preventing duplicate T3 turns.
- **Duplicate suppression (`dedup/`)**: Re-dropping an already-recorded `taskId` into `outbox/` archives it to `dedup/` without calling `t3`.
- **Filesystem hardening**:
  - Runtime `umask(0o077)`, `0o700` queue directories, `0o600` state files.
  - `no_symlink()` walks every parent component; `read_bytes()` opens with `O_RDONLY | O_NOFOLLOW | O_NONBLOCK`, checks `S_ISREG` + `st_nlink == 1` + size cap + unchanged `(st_size, st_mtime_ns)`.
  - `atomic_json()` creates `O_EXCL | O_NOFOLLOW` temp files, calls `fsync()` on file and directory, and atomically replaces targets.
  - Strict JSON decoding rejects duplicate keys and `NaN`/`Infinity` constants.
  - `replyThreadId` is validated against per-pair whitelists (`t3ThreadId`, `defaultReplyThreadId`, `allowedReplyThreadIds`, and enabled pair `t3ThreadId`s).
