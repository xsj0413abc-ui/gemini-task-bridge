# Architecture, Protocol & Safety Invariants

## 1. System Architecture

```text
+-------------------------------------------------------------------------+
| Caller: T3 Code | Claude Code | Codex | Antigravity                     |
|   CLI: t3-gemini-bridge send / task status|wait|read                    |
|   MCP: t3-gemini-bridge mcp (stdlib JSON-RPC 2.0 over stdio)            |
+------------------------------------+------------------------------------+
                                     |
         +---------------------------+---------------------------+
         | effective == "t3" (t3 pair only)                      | effective == "gemini"
         |   Per-task lock + direct_t3_sending -> t3 CLI         |   3-phase hidden staging + atomic_rename_excl
         v                                                       v
+----------------------------------+        +---------------------------------------------+
| Direct T3 Delivery               |        | Atomic File Queue (<root>/inbox/*.task.json)|
|   `t3 thread send --json`        |        +----------------------+----------------------+
+----------------------------------+                               |
                                                                   v
                                            +---------------------------------------------+
                                            | Single LaunchAgent Daemon (2s poll, stdlib) |
                                            |   1. Single-link Python.app TCC binary      |
                                            |   2. Pre-AX `dispatching` + Step C re-lock  |
                                            |   3. Injects via AXValue + AXPress (silent) |
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
                                            | Idempotent DeliveryAdapter Pipeline         |
                                            |   outbox/ -> sending/ -> Adapter.deliver()  |
                                            |   -> payload + sent/<tid>.receipt.json      |
                                            |   -> ledger `sent`/`completed`              |
                                            +---------------------------------------------+
```

### DeliveryAdapters (`t3` and `local`)
- **`T3DeliveryAdapter` (`adapter="t3"`)**: Calls `t3 thread send --base-dir ... --json`, enforces 60s fail-closed `unknown_timeout` on timeout/crash (zero auto-retry), and writes flat `sent/<taskId>.json` + `sent/<taskId>.receipt.json` (`counts["sent"]` +2).
- **`LocalDeliveryAdapter` (`adapter="local"`)**: Zero external CLI subprocesses. Writes verified reply payload to `sent/payloads/<taskId>.json` and receipt to `sent/<taskId>.receipt.json`. `read_task_payload()` verifies basename, `taskId`, `pairId`/`pairUid`, UTF-8 byte count, and SHA-256 via `no_symlink()` + `read_bytes()`.

---

## 2. Bidirectional v2 Upgrade Gate (`require_v21_runtime_for_root`)

Old `v2.0` daemons validate every entry in `pairs.json` (including disabled pairs) before processing any queues. Writing a `local` pair while a `v2.0` daemon runs (or can be relaunched by KeepAlive) halts all `t3` pairs.

Before writing `local` pairs, assigning `pairUid`, using `--idempotency-key`, or starting `mcp`, the bridge verifies:
- **Condition A (Live v2.1 Daemon)**: `.process.lock` is held by PID `P`, `.daemon_health.json` matches `pid == P`, lists `"local_adapter"` in `capabilities`, `bridgeVersion >= 2.1.0`, and has a fresh `lastScanAt` heartbeat.
- **Condition B (Provably Offline / Fresh Root)**: `.process.lock` is NOT held, AND either no LaunchAgent plist targets `root` (and no legacy `.daemon_health.json` lacks `capabilities`), OR every matching LaunchAgent is unloaded (`loaded == False`) and on-disk scripts are verified `>= 2.1.0`.

Plain legacy `T3 send` (when the gate fails, `adapter=="t3"`, no `--idempotency-key`) uses the explicit **Legacy v2.0 Compat Write Path** (single-link `atomic_rename_excl` without pre-writing v2.1 `dispatches` fields that an old daemon would overwrite).

---

## 3. 3-Phase Single-Link Intake & Pre-AX `dispatching` Guard

1. **Single-Link Exclusive Publish (`atomic_rename_excl`)**:
   - Uses macOS `renamex_np(..., RENAME_EXCL)` or Linux `renameat2(..., RENAME_NOREPLACE)` (with `.inbox_publish.lock` fallback on `ENOTSUP`/`ENOSYS`) — never `os.link()`. Published files ALWAYS have `st_nlink == 1`, satisfying `read_bytes()` hardlink checks and rejecting collisions with `EEXIST`.
2. **3-Phase Intake (`submit_task` + `reconcile_staged_intake`)**:
   - Write & `fsync` hidden `inbox/._stage.<taskId>.<token>.task.json` while holding `flock(LOCK_EX | LOCK_NB)` on the staging fd -> commit `dispatches[tid]` (`status="queued", published=False, requestSha256`) under `.pairs.lock -> .ledger.lock` -> publish via `atomic_rename_excl()` -> mark `published=True`.
   - Every `scan()` runs `reconcile_staged_intake()` first to finish unpublished reservations and sweeps unreferenced staging files ONLY after acquiring `flock` (never racing a live writer).
3. **Pre-AX `dispatching` & Step C Re-Verification**:
   - **Step A** (`.gemini_ui.lock`): check window title, busy/card, capture bounded baseline (`<= 64 KiB` / `200` chunks).
   - **Step B** (`.ledger.lock`, `28 MiB` intake cap): commit `status="dispatching"` BEFORE pressing Send, reserving **`4 MiB` headroom** up to `32 MiB` hard limit for terminal/recovery commits within tested concurrency bounds.
   - **Step C** (re-acquire `.gemini_ui.lock`): re-open and re-verify frozen `geminiTitle`, `not busy`, and `not confirmation_card`, then invoke `write_prompt_and_submit()` inside that same lock.
   - Only proven pre-press failures (`PRE_SUBMIT_REVERT_REASONS`) revert `dispatching -> queued`. `submit_press_uncertain` (when `AXPress` was invoked) and `submit_unconfirmed` stay in `dispatching` with zero auto-repress and are reconciled by `reconcile_dispatching()` in `scan()` and `recover()` via `sent` -> `outbox`/`sending` -> AX `[[T3_BRIDGE_START:<tid>]]` sentinel -> `unknown_timeout`.

---

## 4. Ownership, `pairUid`, Stage Gate & MCP ACL

- **Fixed Lock Order**: `.config.lock -> .pairs.lock -> .ledger.lock -> .gemini_ui.lock` across all code paths.
- **Immutable `pairUid`**: `pair init` always mints a new 32-hex `pairUid`; `pair add` mints a new `pairUid` whenever binding identity (`adapter`, `geminiTitle`, `t3ThreadId`, `defaultReplyThreadId`) changes or after `pair remove`.
- **Stage-Gated Outbox Validation**: Registered tasks accept Gemini outbox receipts ONLY when `deliveryKind == "gemini_reply"` and `dispatches[tid].status in ("dispatching", "in_flight", "needs_attention", "captured_to_outbox")`, cross-checked against frozen intake snapshots (`pairId`, `pairUid`, `adapter`, `replyThreadId`). Quarantine (`failed/`) never mutates existing `tasks` or `dispatches` entries.
- **Legacy `pairUid=None` Policy**: Historical tasks with `pairUid=None` are verified by `(pairId, geminiTitle, authorized replyThreadId)` (checking `sourceT3ThreadId` only when present) so `--reply-to` (`source A != reply B`) continues working; restricted MCP never grants retroactive read access to `pairUid=None` history.
- **MCP Scope Boundary**: `McpScope` defaults to `--adapter-filter local`, fails closed on empty/invalid `--allow-pairs`, authorizes every `read`/`wait`/`send(wait=true)`/`idempotency` hit via `authorize_task()`, and removes global sidebar enumeration (`gemini_health` reports only scoped counts). Note: this ACL isolates the MCP server protocol boundary, not separate OS-level filesystem permissions under the same macOS user account.

---

## 5. Unified `needs_attention` & Crash Recovery

- Confirmed `confirmation_card` takes precedence over `in_progress` badge in the same sample; unverified `outbox/` is pending, not terminal.
- Exiting `needs_attention -> in_flight` requires BOTH `badge != "needs_attention"` AND verified `poll_conversation()` (`window_title == geminiTitle` and `not confirmation_card`), resetting `notifiedAttention=False` and poll/stability counters; unreliable probes (screen lock, AX error, title mismatch) never clear `needs_attention`.
- `recover()` resolves `sending/` strictly across all 5 cutpoints `(b) committed sent -> (c) persisted verified t3 receipt -> (a) pre-claim sending -> (d) local sending -> (e) t3 unknown_timeout` without ever auto-resending to T3, and re-verifies `direct_t3_sending` inside `ledger_tx()` before probing per-task caller locks (`locks/direct_t3.<tid>.lock`).
