#!/usr/bin/python3
"""Zero-dependency Python 3.9+ stdlib MCP stdio server for t3-gemini-bridge."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
import sys
import threading
from typing import Any

from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import t3_gemini_bridge as tb  # noqa: E402

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_MCP_LINE_BYTES = 512 * 1024
MAX_CONCURRENT_WAITS = 8
FAST_POOL_WORKERS = 4
FAST_POOL_PENDING_CAP = 20

CONTROL_METHODS = frozenset({
    "initialize",
    "notifications/initialized",
    "notifications/cancelled",
    "ping",
    "tools/list",
})


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class McpScope:
    """Fail-closed per-instance scope enforcement for MCP tool calls."""

    def __init__(self, adapter_filter: str = "local", allow_pairs: set[str] | None = None) -> None:
        if adapter_filter not in ("local", "t3", "all"):
            raise tb.BridgeError("invalid_mcp_adapter_filter")
        self.adapter_filter = adapter_filter
        self.allow_pairs = allow_pairs

    @classmethod
    def from_args(cls, allow_pairs_raw: str | None, adapter_filter_raw: str | None) -> McpScope:
        if adapter_filter_raw is None:
            env_af = os.environ.get("T3_GEMINI_BRIDGE_MCP_ADAPTER_FILTER")
            adapter_filter = env_af.strip() if env_af is not None else "local"
        else:
            adapter_filter = adapter_filter_raw.strip()
        if adapter_filter not in ("local", "t3", "all"):
            raise tb.BridgeError("invalid_mcp_adapter_filter")

        raw_allow = allow_pairs_raw
        if raw_allow is None and "T3_GEMINI_BRIDGE_MCP_ALLOW_PAIRS" in os.environ:
            raw_allow = os.environ["T3_GEMINI_BRIDGE_MCP_ALLOW_PAIRS"]

        allow_set: set[str] | None = None
        if raw_allow is not None:
            stripped = raw_allow.strip()
            if not stripped:
                raise tb.BridgeError("invalid_mcp_allow_pairs")
            parts = stripped.split(",")
            allow_set = set()
            for part in parts:
                tok = part.strip()
                if not tok or not tb.PAIR_ID_RE.fullmatch(tok):
                    raise tb.BridgeError("invalid_mcp_allow_pairs")
                allow_set.add(tok)
        return cls(adapter_filter=adapter_filter, allow_pairs=allow_set)

    def allowed_adapters(self) -> frozenset[str]:
        if self.adapter_filter == "all":
            return frozenset({"local", "t3"})
        return frozenset({self.adapter_filter})

    def adapter_allowed(self, adapter: str) -> bool:
        if self.adapter_filter == "all":
            return adapter in ("local", "t3")
        return adapter == self.adapter_filter

    def pair_allowed(self, pair_id: str, pair: dict[str, Any] | None) -> bool:
        if not isinstance(pair, dict) or not pair.get("enabled", True):
            return False
        if not self.adapter_allowed(tb.pair_adapter(pair)):
            return False
        if self.allow_pairs is not None and pair_id not in self.allow_pairs:
            return False
        return True

    def authorize_task(self, task_id: str, pairs_data: dict[str, Any], ledger_data: dict[str, Any]) -> tuple[bool, str | None]:
        t_rec = ledger_data["tasks"].get(task_id)
        d_rec = ledger_data["dispatches"].get(task_id)
        src = d_rec if isinstance(d_rec, dict) else t_rec
        if not isinstance(src, dict):
            return False, None
        if (isinstance(t_rec, dict) and t_rec.get("unregisteredReceipt")) or src.get("unregisteredReceipt"):
            return False, None
        pair_id = src.get("pairId")
        pair_uid = src.get("pairUid")
        adapter = src.get("adapter")
        if not isinstance(pair_id, str) or not isinstance(pair_uid, str) or not tb.PAIR_UID_RE.fullmatch(pair_uid):
            return False, None
        if adapter not in ("local", "t3") or not self.adapter_allowed(adapter):
            return False, None
        pair = pairs_data["pairs"].get(pair_id)
        if not self.pair_allowed(pair_id, pair):
            return False, None
        if pair.get("pairUid") != pair_uid:
            return False, None
        return True, pair_id


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "gemini_health",
        "description": "Check LaunchAgent liveness, macOS Accessibility trust, screen lock state, Gemini.app status, and scoped pair/task counts.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "gemini_pairs",
        "description": "List enabled Gemini bridge pairs accessible within this MCP server's configured scope.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "gemini_send",
        "description": "Submit a task to a scoped Gemini bridge pair. Defaults to non-blocking immediate return with durable taskId.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pair_id": {"type": "string", "minLength": 1},
                "message": {"type": "string", "minLength": 1},
                "wait": {"type": "boolean"},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 300},
                "return_on_attention": {"type": "boolean"},
                "idempotency_key": {"type": "string", "minLength": 1},
            },
            "required": ["pair_id", "message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "gemini_wait",
        "description": "Wait in bounded chunks for a previously submitted scoped taskId to finish or request manual confirmation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "minLength": 1},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 300},
                "return_on_attention": {"type": "boolean"},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "gemini_read",
        "description": "Read the verified reply payload and receipt for a completed scoped taskId.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "minLength": 1},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
]
TOOLS_BY_NAME: dict[str, dict[str, Any]] = {spec["name"]: spec for spec in TOOL_SPECS}


def _validate_tool_args(tool_name: str, raw_args: Any) -> dict[str, Any]:
    if tool_name not in TOOLS_BY_NAME:
        raise JsonRpcError(-32602, f"Unknown tool: {tool_name}")
    if raw_args is None:
        args: dict[str, Any] = {}
    elif isinstance(raw_args, dict):
        args = raw_args
    else:
        raise JsonRpcError(-32602, "Tool arguments must be an object")

    schema = TOOLS_BY_NAME[tool_name]["inputSchema"]
    props = schema["properties"]
    required = schema.get("required", [])
    for key in args:
        if key not in props:
            raise JsonRpcError(-32602, f"Unexpected argument: {key}")
    for req in required:
        if req not in args:
            raise JsonRpcError(-32602, f"Missing required argument: {req}")

    for key, val in args.items():
        ps = props[key]
        ptype = ps["type"]
        if ptype == "string":
            if not isinstance(val, str) or len(val) < ps.get("minLength", 0):
                raise JsonRpcError(-32602, f"Invalid string argument: {key}")
        elif ptype == "boolean":
            if not isinstance(val, bool):
                raise JsonRpcError(-32602, f"Invalid boolean argument: {key}")
        elif ptype == "number":
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val):
                raise JsonRpcError(-32602, f"Invalid number argument: {key}")
            if "minimum" in ps and val < ps["minimum"]:
                raise JsonRpcError(-32602, f"Argument out of range (min): {key}")
            if "maximum" in ps and val > ps["maximum"]:
                raise JsonRpcError(-32602, f"Argument out of range (max): {key}")
    return args


def _req_id_key(req_id: Any) -> tuple[str, Any]:
    return ("s" if isinstance(req_id, str) else "i", req_id)


def _valid_request_id(req_id: Any) -> bool:
    if isinstance(req_id, bool) or req_id is None:
        return False
    return isinstance(req_id, (str, int))


class McpStdioServer:
    def __init__(self, bridge: tb.Bridge, scope: McpScope, raw_stdin: Any, raw_stdout: Any) -> None:
        self.bridge = bridge
        self.scope = scope
        self.raw_stdin = raw_stdin
        self.raw_stdout = raw_stdout
        self.write_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.negotiated_version: str | None = None
        self.initialized = False
        self.shutdown_event = threading.Event()
        self.inflight: dict[tuple[str, Any], threading.Event] = {}
        self.fast_sem = threading.BoundedSemaphore(FAST_POOL_PENDING_CAP)
        self.wait_sem = threading.BoundedSemaphore(MAX_CONCURRENT_WAITS)
        self.fast_pool = ThreadPoolExecutor(max_workers=FAST_POOL_WORKERS, thread_name_prefix="mcp_fast")
        self.wait_pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_WAITS, thread_name_prefix="mcp_wait")

    def send_frame(self, obj: Any) -> None:
        encoded = (json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        with self.write_lock:
            self.raw_stdout.write(encoded)
            self.raw_stdout.flush()

    def _make_error(self, req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
        err: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return {"jsonrpc": "2.0", "id": req_id, "error": err}

    def _format_tool_result(self, payload: dict[str, Any], is_error: bool) -> dict[str, Any]:
        res: dict[str, Any] = {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": is_error,
        }
        if self.negotiated_version == "2025-06-18" and not is_error:
            res["structuredContent"] = payload
        return res

    def _tool_health(self) -> tuple[bool, dict[str, Any]]:
        cfg = self.bridge.config()
        pairs_data = self.bridge.pairs()
        with tb.file_lock(self.bridge.root / ".ledger.lock"):
            ledger_data = self.bridge.ledger()
        probes = self.bridge.system_probes()
        health = self.bridge.read_health() or {}
        agent = tb.agent_info()

        scoped_pairs = {
            pid: p for pid, p in pairs_data["pairs"].items()
            if self.scope.pair_allowed(pid, p)
        }
        scoped_attention_pairs: list[dict[str, Any]] = []
        scoped_active_tasks = 0
        for tid in ledger_data["dispatches"]:
            ok_auth, pid = self.scope.authorize_task(tid, pairs_data, ledger_data)
            if not ok_auth or pid is None:
                continue
            d = ledger_data["dispatches"][tid]
            st = d.get("status")
            if st in tb.ACTIVE_DISPATCH_STATUSES:
                scoped_active_tasks += 1
            if st == "needs_attention":
                scoped_attention_pairs.append({
                    "pairId": pid,
                    "taskId": tid,
                    "geminiTitle": scoped_pairs[pid]["geminiTitle"],
                    "attentionEpisode": d.get("attentionEpisode", 0),
                })

        daemon_running = bool(agent.get("loaded") and agent.get("state") == "running")
        payload = {
            "ok": True,
            "bridgeVersion": tb.BRIDGE_VERSION,
            "daemonRunning": daemon_running,
            "axTrusted": bool(health.get("axTrusted") if health else probes.get("axTrusted")),
            "screenLocked": bool(health.get("screenLocked") if health else probes.get("screenLocked")),
            "geminiRunning": bool(health.get("geminiRunning") if health else probes.get("geminiRunning")),
            "globalMode": cfg["global_mode"],
            "lastScanAt": health.get("lastScanAt"),
            "lastErrorPresent": bool(health.get("lastError")),
            "scopedPairCount": len(scoped_pairs),
            "scopedActiveTaskCount": scoped_active_tasks,
            "scopedNeedsAttentionPairs": scoped_attention_pairs,
        }
        return False, payload

    def _tool_pairs(self) -> tuple[bool, dict[str, Any]]:
        cfg = self.bridge.config()
        pairs_data = self.bridge.pairs()
        in_flight = self.bridge._active_in_flight_by_pair()
        out = []
        for pid, p in sorted(pairs_data["pairs"].items()):
            if not self.scope.pair_allowed(pid, p):
                continue
            adapter = tb.pair_adapter(p)
            item: dict[str, Any] = {
                "pairId": pid,
                "pairUid": p.get("pairUid"),
                "adapter": adapter,
                "label": p.get("label"),
                "geminiTitle": p["geminiTitle"],
                "workspacePath": p.get("workspacePath"),
                "effectiveRoute": tb.resolve_effective_route(cfg, p),
                "replyMode": p.get("replyMode", "auto"),
                "inFlightTaskId": in_flight.get(pid),
            }
            if adapter == "t3":
                item["t3ThreadId"] = p.get("t3ThreadId")
                item["defaultReplyThreadId"] = p.get("defaultReplyThreadId")
            out.append(item)
        return False, {"ok": True, "globalMode": cfg["global_mode"], "pairs": out}

    def _authorize_or_error(self, task_id: str) -> tuple[bool, dict[str, Any] | None]:
        pairs_data = self.bridge.pairs()
        with tb.file_lock(self.bridge.root / ".ledger.lock"):
            ledger_data = self.bridge.ledger()
        ok_auth, _ = self.scope.authorize_task(task_id, pairs_data, ledger_data)
        if not ok_auth:
            return False, {"ok": False, "taskId": task_id, "error": "task_not_accessible_in_scope"}
        return True, None

    def _tool_send(self, args: dict[str, Any], cancel_event: threading.Event) -> tuple[bool, dict[str, Any]]:
        pair_id = args["pair_id"]
        if not tb.PAIR_ID_RE.fullmatch(pair_id):
            return True, {"ok": False, "error": "invalid_pair_id"}
        pairs_data = self.bridge.pairs()
        pair = pairs_data["pairs"].get(pair_id)
        if not self.scope.pair_allowed(pair_id, pair):
            return True, {"ok": False, "error": "pair_not_accessible_in_scope"}
        expected_pair_uid = pair.get("pairUid")
        expected_adapter = tb.pair_adapter(pair)
        expected_gemini_title = pair["geminiTitle"]

        wait_flag = bool(args.get("wait", False))
        timeout_s = float(args.get("timeout_seconds", 30.0))
        ret_att = bool(args.get("return_on_attention", True))
        idem_key = args.get("idempotency_key")

        try:
            res = self.bridge.submit_task(
                pair_id=pair_id,
                message=args["message"],
                idempotency_key=idem_key,
                forbid_legacy_pair_uid_deferral=True,
                allowed_adapters=self.scope.allowed_adapters(),
                allow_pairs=self.scope.allow_pairs,
                expected_pair_uid=expected_pair_uid,
                expected_adapter=expected_adapter,
                expected_gemini_title=expected_gemini_title,
                require_v21_runtime=True,
            )
        except tb.BridgeError as exc:
            return True, {"ok": False, "error": str(exc)}

        tid = res["taskId"]
        ok_auth, deny_payload = self._authorize_or_error(tid)
        if not ok_auth and deny_payload is not None:
            return True, deny_payload

        if res.get("routed") == "t3" and not res.get("idempotentReplay"):
            return False, {
                "ok": True,
                "completed": True,
                "routed": "t3",
                "taskId": tid,
                "pairId": pair_id,
                "deliveryKind": "direct_t3_forwarded",
                "hasReply": False,
                "threadId": res.get("threadId"),
                "t3": res.get("t3"),
            }

        is_replay = bool(res.get("idempotentReplay"))

        def _with_replay(out_payload: dict[str, Any]) -> dict[str, Any]:
            if is_replay:
                out_payload["idempotentReplay"] = True
            return out_payload

        if not wait_flag:
            st = self.bridge.get_task_status(tid)
            is_terminal_err = st["status"] in ("failed", "timed_out", "unknown_timeout")
            if is_terminal_err:
                return True, {"ok": False, "completed": True, "taskId": tid, "pairId": pair_id, "status": st["status"], "reason": st.get("reason")}
            if st["status"] == "sent":
                try:
                    p = self.bridge.read_task_payload(tid)
                    return False, _with_replay({"ok": True, "completed": True, "taskId": tid, "pairId": pair_id, "status": "sent", "message": p["message"], "receipt": p.get("receipt")})
                except tb.BridgeError as exc:
                    return True, {"ok": False, "taskId": tid, "error": str(exc)}
            if st["status"] == "direct_t3":
                return False, _with_replay({"ok": True, "completed": True, "routed": "t3", "taskId": tid, "pairId": pair_id, "status": "direct_t3", "deliveryKind": "direct_t3_forwarded", "hasReply": False})
            return False, _with_replay({
                "ok": True,
                "completed": False,
                "taskId": tid,
                "pairId": pair_id,
                "status": st["status"],
                "nextAction": "call gemini_wait with task_id",
            })

        rc, wait_out = self.bridge.wait_for_task(
            tid,
            timeout_s=timeout_s,
            return_on_attention=ret_att,
            cancel_event=cancel_event,
            shutdown_event=self.shutdown_event,
        )
        ok_auth2, deny2 = self._authorize_or_error(tid)
        if not ok_auth2 and deny2 is not None:
            return True, deny2
        if rc in (1, 3):
            wait_out["ok"] = True
            wait_out["completed"] = False
            wait_out["nextAction"] = (
                "ask user to confirm in Gemini.app then call gemini_wait with task_id"
                if rc == 3
                else "call gemini_wait with task_id"
            )
            return False, _with_replay(wait_out)
        if rc == 0:
            return False, _with_replay(wait_out)
        return True, wait_out

    def _tool_wait(self, args: dict[str, Any], cancel_event: threading.Event) -> tuple[bool, dict[str, Any]]:
        tid = args["task_id"]
        if not tb.TASK_ID_RE.fullmatch(tid):
            return True, {"ok": False, "error": "invalid_task_id"}
        ok_auth, deny_payload = self._authorize_or_error(tid)
        if not ok_auth and deny_payload is not None:
            return True, deny_payload

        timeout_s = float(args.get("timeout_seconds", 30.0))
        ret_att = bool(args.get("return_on_attention", True))
        rc, out = self.bridge.wait_for_task(
            tid,
            timeout_s=timeout_s,
            return_on_attention=ret_att,
            cancel_event=cancel_event,
            shutdown_event=self.shutdown_event,
        )
        ok_auth2, deny2 = self._authorize_or_error(tid)
        if not ok_auth2 and deny2 is not None:
            return True, deny2
        if rc in (1, 3):
            out["ok"] = True
            out["completed"] = False
            out["nextAction"] = (
                "ask user to confirm in Gemini.app then call gemini_wait with task_id"
                if rc == 3
                else "call gemini_wait with task_id"
            )
            return False, out
        if rc == 0:
            return False, out
        return True, out

    def _tool_read(self, args: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        tid = args["task_id"]
        if not tb.TASK_ID_RE.fullmatch(tid):
            return True, {"ok": False, "error": "invalid_task_id"}
        ok_auth, deny_payload = self._authorize_or_error(tid)
        if not ok_auth and deny_payload is not None:
            return True, deny_payload
        try:
            res = self.bridge.read_task_payload(tid)
            return False, res
        except tb.BridgeError as exc:
            return True, {"ok": False, "taskId": tid, "error": str(exc)}

    def _execute_tool(self, tool_name: str, args: dict[str, Any], cancel_event: threading.Event) -> tuple[bool, dict[str, Any]]:
        try:
            if tool_name == "gemini_health":
                return self._tool_health()
            if tool_name == "gemini_pairs":
                return self._tool_pairs()
            if tool_name == "gemini_send":
                return self._tool_send(args, cancel_event)
            if tool_name == "gemini_wait":
                return self._tool_wait(args, cancel_event)
            if tool_name == "gemini_read":
                return self._tool_read(args)
            return True, {"ok": False, "error": "unknown_tool"}
        except tb.BridgeError as exc:
            return True, {"ok": False, "error": str(exc)}
        except Exception as exc:
            return True, {"ok": False, "error": f"{type(exc).__name__}:{exc}"}

    def _is_blocking_tool(self, tool_name: str, args: dict[str, Any]) -> bool:
        if tool_name == "gemini_wait":
            return True
        if tool_name == "gemini_send":
            if bool(args.get("wait", False)):
                return True
            try:
                cfg = self.bridge.config()
                pairs_data = self.bridge.pairs()
                pair = pairs_data["pairs"].get(args["pair_id"])
                if isinstance(pair, dict) and self.scope.pair_allowed(args["pair_id"], pair) and tb.resolve_effective_route(cfg, pair) == "t3":
                    return True
            except Exception:
                pass
        return False

    def _handle_control_or_sync(self, msg: dict[str, Any]) -> tuple[bool, Any]:
        """Handle control frames inline on reader thread; return (has_response, response_obj)."""
        has_id = "id" in msg
        req_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params")

        if not has_id:
            if method == "notifications/initialized":
                return False, None
            if method == "notifications/cancelled":
                if isinstance(params, dict):
                    target_id = params.get("requestId")
                    if _valid_request_id(target_id):
                        key = _req_id_key(target_id)
                        with self.state_lock:
                            ev = self.inflight.get(key)
                        if ev is not None:
                            ev.set()
                return False, None
            return False, None

        if method == "initialize":
            if params is not None and not isinstance(params, dict):
                return True, self._make_error(req_id, -32602, "Invalid params")
            req_ver = params.get("protocolVersion") if isinstance(params, dict) else None
            if req_ver is not None and not isinstance(req_ver, str):
                return True, self._make_error(req_id, -32602, "Invalid protocolVersion")
            chosen = req_ver if req_ver in SUPPORTED_VERSIONS else "2025-06-18"
            with self.state_lock:
                self.negotiated_version = chosen
                self.initialized = True
            return True, {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": chosen,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "t3-gemini-bridge", "version": tb.BRIDGE_VERSION},
                },
            }

        if method == "ping":
            return True, {"jsonrpc": "2.0", "id": req_id, "result": {}}

        if not self.initialized:
            return True, self._make_error(req_id, -32002, "Server not initialized")

        if method == "tools/list":
            if params is not None and not isinstance(params, dict):
                return True, self._make_error(req_id, -32602, "Invalid params")
            return True, {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOL_SPECS}}

        return True, self._make_error(req_id, -32601, f"Method not found: {method}")

    def _validate_envelope(self, msg: Any) -> tuple[bool, dict[str, Any] | None]:
        """Validate basic JSON-RPC 2.0 request/notification structure."""
        if not isinstance(msg, dict):
            return False, self._make_error(None, -32600, "Invalid Request")
        if msg.get("jsonrpc") != "2.0":
            err_id = msg.get("id") if _valid_request_id(msg.get("id")) else None
            return False, self._make_error(err_id, -32600, "Invalid Request: jsonrpc must be '2.0'")
        if "id" in msg and not _valid_request_id(msg["id"]):
            return False, self._make_error(None, -32600, "Invalid Request: invalid id")
        method = msg.get("method")
        if not isinstance(method, str) or not method:
            err_id = msg.get("id") if "id" in msg else None
            return False, self._make_error(err_id, -32600, "Invalid Request: invalid method")
        return True, None

    def _prepare_tool_call(self, msg: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if not self.initialized:
            raise JsonRpcError(-32002, "Server not initialized")
        params = msg.get("params")
        if not isinstance(params, dict):
            raise JsonRpcError(-32602, "tools/call params must be an object")
        tool_name = params.get("name")
        if not isinstance(tool_name, str):
            raise JsonRpcError(-32602, "Tool name must be a string")
        args = _validate_tool_args(tool_name, params.get("arguments"))
        return tool_name, args

    def _dispatch_single_async(self, msg: dict[str, Any]) -> None:
        ok_env, err_obj = self._validate_envelope(msg)
        if not ok_env:
            if err_obj is not None:
                self.send_frame(err_obj)
            return

        method = msg["method"]
        if "id" not in msg or method in CONTROL_METHODS:
            has_resp, resp = self._handle_control_or_sync(msg)
            if has_resp and resp is not None:
                self.send_frame(resp)
            return

        req_id = msg["id"]
        if method != "tools/call":
            self.send_frame(self._make_error(req_id, -32601, f"Method not found: {method}"))
            return

        try:
            tool_name, args = self._prepare_tool_call(msg)
        except JsonRpcError as exc:
            self.send_frame(self._make_error(req_id, exc.code, exc.message, exc.data))
            return

        blocking = self._is_blocking_tool(tool_name, args)
        sem = self.wait_sem if blocking else self.fast_sem
        pool = self.wait_pool if blocking else self.fast_pool
        if not sem.acquire(blocking=False):
            err_code = "too_many_concurrent_waits" if blocking else "mcp_server_busy"
            self.send_frame({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": self._format_tool_result({"ok": False, "error": err_code}, is_error=True),
            })
            return

        req_key = _req_id_key(req_id)
        cancel_event = threading.Event()
        with self.state_lock:
            self.inflight[req_key] = cancel_event

        def _worker() -> None:
            try:
                is_err, payload = self._execute_tool(tool_name, args, cancel_event)
                if cancel_event.is_set() or self.shutdown_event.is_set():
                    return
                self.send_frame({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": self._format_tool_result(payload, is_error=is_err),
                })
            finally:
                with self.state_lock:
                    self.inflight.pop(req_key, None)
                sem.release()

        try:
            pool.submit(_worker)
        except Exception:
            with self.state_lock:
                self.inflight.pop(req_key, None)
            sem.release()
            self.send_frame(self._make_error(req_id, -32603, "Internal error"))

    def _handle_batch(self, items: list[Any]) -> None:
        if self.negotiated_version != "2025-03-26" or len(items) == 0:
            self.send_frame(self._make_error(None, -32600, "Invalid Request"))
            return

        batch_lock = threading.Lock()
        results: list[Any] = [None] * len(items)
        state: dict[str, Any] = {"remaining": 0, "sealed": False, "sent": False}

        def _take_ready_payload() -> list[Any] | None:
            if state["sealed"] and state["remaining"] == 0 and not state["sent"]:
                state["sent"] = True
                return [r for r in results if r is not None]
            return None

        for idx, msg in enumerate(items):
            ok_env, err_obj = self._validate_envelope(msg)
            if not ok_env:
                results[idx] = err_obj
                continue
            method = msg["method"]
            if "id" not in msg or method in CONTROL_METHODS:
                has_resp, resp = self._handle_control_or_sync(msg)
                results[idx] = resp if has_resp else None
                continue
            req_id = msg["id"]
            if method != "tools/call":
                results[idx] = self._make_error(req_id, -32601, f"Method not found: {method}")
                continue
            try:
                tool_name, args = self._prepare_tool_call(msg)
            except JsonRpcError as exc:
                results[idx] = self._make_error(req_id, exc.code, exc.message, exc.data)
                continue

            blocking = self._is_blocking_tool(tool_name, args)
            sem = self.wait_sem if blocking else self.fast_sem
            pool = self.wait_pool if blocking else self.fast_pool
            if not sem.acquire(blocking=False):
                err_code = "too_many_concurrent_waits" if blocking else "mcp_server_busy"
                results[idx] = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": self._format_tool_result({"ok": False, "error": err_code}, is_error=True),
                }
                continue

            req_key = _req_id_key(req_id)
            cancel_event = threading.Event()
            with self.state_lock:
                self.inflight[req_key] = cancel_event
            with batch_lock:
                state["remaining"] += 1

            def _batch_worker(
                slot: int = idx,
                rid: Any = req_id,
                rkey: tuple[str, Any] = req_key,
                tname: str = tool_name,
                targs: dict[str, Any] = args,
                cev: threading.Event = cancel_event,
                s_sem: threading.BoundedSemaphore = sem,
            ) -> None:
                ready_out: list[Any] | None = None
                try:
                    is_err, payload = self._execute_tool(tname, targs, cev)
                    if not cev.is_set() and not self.shutdown_event.is_set():
                        with batch_lock:
                            results[slot] = {
                                "jsonrpc": "2.0",
                                "id": rid,
                                "result": self._format_tool_result(payload, is_error=is_err),
                            }
                finally:
                    with self.state_lock:
                        if self.inflight.get(rkey) is cev:
                            self.inflight.pop(rkey, None)
                    s_sem.release()
                    with batch_lock:
                        state["remaining"] -= 1
                        ready_out = _take_ready_payload()
                if ready_out and not self.shutdown_event.is_set():
                    self.send_frame(ready_out)

            try:
                pool.submit(_batch_worker)
            except Exception:
                with self.state_lock:
                    if self.inflight.get(req_key) is cancel_event:
                        self.inflight.pop(req_key, None)
                sem.release()
                ready_out_err: list[Any] | None = None
                with batch_lock:
                    results[idx] = self._make_error(req_id, -32603, "Internal error")
                    state["remaining"] -= 1
                    ready_out_err = _take_ready_payload()
                if ready_out_err and not self.shutdown_event.is_set():
                    self.send_frame(ready_out_err)

        final_out: list[Any] | None = None
        with batch_lock:
            state["sealed"] = True
            final_out = _take_ready_payload()
        if final_out and not self.shutdown_event.is_set():
            self.send_frame(final_out)

    def serve(self) -> int:
        try:
            while not self.shutdown_event.is_set():
                raw_line = self.raw_stdin.readline(MAX_MCP_LINE_BYTES + 1)
                if not raw_line:
                    break
                if len(raw_line) > MAX_MCP_LINE_BYTES and not raw_line.endswith(b"\n"):
                    # Drain rest of oversized line without buffering unboundedly
                    while True:
                        chunk = self.raw_stdin.readline(MAX_MCP_LINE_BYTES)
                        if not chunk or chunk.endswith(b"\n"):
                            break
                    self.send_frame(self._make_error(None, -32600, "Invalid Request: frame too large"))
                    continue
                if len(raw_line) > MAX_MCP_LINE_BYTES:
                    self.send_frame(self._make_error(None, -32600, "Invalid Request: frame too large"))
                    continue
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    text = stripped.decode("utf-8")
                    frame = tb.decode_json(text)
                except (UnicodeDecodeError, tb.BridgeError):
                    self.send_frame(self._make_error(None, -32700, "Parse error"))
                    continue

                if isinstance(frame, list):
                    self._handle_batch(frame)
                elif isinstance(frame, dict):
                    self._dispatch_single_async(frame)
                else:
                    self.send_frame(self._make_error(None, -32600, "Invalid Request"))
        finally:
            self.shutdown_event.set()
            with self.state_lock:
                for ev in self.inflight.values():
                    ev.set()
            self.fast_pool.shutdown(wait=True)
            self.wait_pool.shutdown(wait=True)
        return 0


def run_stdio_server(
    bridge: tb.Bridge,
    allow_pairs_raw: str | None = None,
    adapter_filter: str | None = None,
) -> int:
    bridge.setup()
    cfg = bridge.config()
    bridge.require_v21_runtime_for_root(cfg)
    scope = McpScope.from_args(allow_pairs_raw, adapter_filter)

    raw_stdin = sys.stdin.buffer
    raw_stdout = sys.stdout.buffer
    # Redirect sys.stdout to sys.stderr so accidental prints never corrupt JSON-RPC framing
    sys.stdout = sys.stderr

    server = McpStdioServer(bridge=bridge, scope=scope, raw_stdin=raw_stdin, raw_stdout=raw_stdout)
    return server.serve()


if __name__ == "__main__":
    os.umask(0o077)
    bridge = tb.Bridge(root=tb.DEFAULT_ROOT)
    sys.exit(run_stdio_server(bridge))
