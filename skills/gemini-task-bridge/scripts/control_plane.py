"""Durable daemon control requests. Clients publish and wait without using AX."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Any, Callable
import uuid

import gemini_task_bridge as tb

CAPABILITY = "daemon_control_plane"
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESULT_BYTES = 2 * 1024 * 1024
MAX_PAIRS_BYTES = 512 * 1024
IN_FLIGHT_META_BYTES = 32
MAX_RESULTS = 500
RETENTION_SECONDS = 7 * 24 * 60 * 60
PAIR_LIMIT = 10
KINDS = ("list_chats", "create_pair")
TERMINAL = ("ok", "failed", "unconfirmed")


def setup(bridge: tb.Bridge) -> None:
    root = bridge.root / "control"
    for path in (root, *(root / name for name in ("inbox", "in_flight", "done", "keys"))):
        tb.no_symlink(path)
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise tb.BridgeError("control_plane_unavailable")


def _parse_stamp(value: Any) -> datetime.datetime | None:
    try:
        return tb.parse_iso_utc(value)
    except OverflowError:
        return None


def health_fresh(health: dict[str, Any], ttl: float) -> bool:
    now = datetime.datetime.now(datetime.timezone.utc)
    busy_until = _parse_stamp(health.get("busyUntil"))
    if busy_until is not None and busy_until > now:
        return True
    stamp = _parse_stamp(health.get("lastScanAt"))
    return stamp is not None and 0 <= (now - stamp).total_seconds() <= ttl


def daemon_available(bridge: tb.Bridge, cfg: dict[str, Any]) -> bool:
    try:
        health = _read(bridge.root / ".daemon_health.json", MAX_REQUEST_BYTES)
    except (OSError, tb.BridgeError):
        return False
    if not isinstance(health, dict) or health.get("axTrusted") is not True:
        return False
    caps = health.get("capabilities")
    return isinstance(caps, list) and CAPABILITY in caps and health_fresh(health, 3 * cfg["poll_interval_seconds"] + 10)


def validate_scope(scope: Any) -> None:
    if not isinstance(scope, str) or not tb.MCP_SCOPE_RE.fullmatch(scope):
        raise tb.BridgeError("invalid_mcp_scope")


def normalized_title(title: Any) -> str:
    if not isinstance(title, str) or not title.strip() or any(ord(c) < 32 or ord(c) == 127 for c in title):
        raise tb.BridgeError("invalid_pair_gemini_title")
    return title.strip()


def title_pair_id(scope: str, title: str) -> str:
    suffix = hashlib.sha256(title.encode()).hexdigest()[:6]
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:63 - len(scope) - 7].rstrip("-")
    return scope + "-" + (slug + "-" if slug else "") + suffix


def title_key(scope: str, title: str) -> str:
    return hashlib.sha256(f"{scope}|{title}".encode()).hexdigest()[:32]


def _key_path(root: Path, scope: str | None, key: str) -> Path:
    return root / "keys" / hashlib.sha256(f"{scope or ''}|{key}".encode()).hexdigest()


def _path(root: Path, state: str, rid: str) -> Path:
    return root / state / (rid + ".json")


def _read(path: Path, limit: int) -> Any:
    return tb.decode_json(tb.read_bytes(path, limit, private=True))


def _read_key(path: Path) -> dict[str, Any] | None:
    try:
        key = _read(path, 1024)
    except FileNotFoundError:
        return None
    if (not isinstance(key, dict) or not isinstance(key.get("requestId"), str)
            or not tb.PAIR_UID_RE.fullmatch(key["requestId"]) or key.get("kind") not in KINDS
            or ("retired" in key and key["retired"] not in TERMINAL)):
        raise tb.BridgeError("invalid_control_request")
    return key


def _validate_request(data: Any, rid: str, *, in_flight: bool = False) -> dict[str, Any]:
    fields = {"schemaVersion", "requestId", "kind", "idempotencyKey", "source", "createdAt", "params"}
    if not isinstance(data, dict) or (set(data) - fields - ({"axReady"} if in_flight else set())) or fields - set(data):
        raise tb.BridgeError("invalid_control_request")
    source, key = data["source"], data["idempotencyKey"]
    if (type(data["schemaVersion"]) is not int or data["schemaVersion"] != 1 or data["requestId"] != rid
            or data["kind"] not in KINDS or _parse_stamp(data["createdAt"]) is None
            or not isinstance(key, str) or not tb.IDEMPOTENCY_KEY_RE.fullmatch(key)
            or not isinstance(source, dict) or set(source) != {"scope", "via"}
            or source["via"] not in ("cli", "mcp")
            or ("axReady" in data and (data["kind"] != "create_pair" or data["axReady"] is not True))
            or not isinstance(data["params"], dict)):
        raise tb.BridgeError("invalid_control_request")
    if source["via"] == "mcp":
        validate_scope(source["scope"])
    elif source["scope"] is not None:
        raise tb.BridgeError("invalid_control_request")
    if data["kind"] == "list_chats" and data["params"]:
        raise tb.BridgeError("invalid_control_request")
    return data


def _read_request(root: Path, path: Path, rid: str, *, in_flight: bool = False) -> dict[str, Any]:
    limit = MAX_REQUEST_BYTES + IN_FLIGHT_META_BYTES if in_flight else MAX_REQUEST_BYTES
    req = _validate_request(_read(path, limit), rid, in_flight=in_flight)
    key = _read_key(_key_path(root, req["source"]["scope"], req["idempotencyKey"]))
    if not key or key.get("retired") or key["requestId"] != rid or key["kind"] != req["kind"]:
        raise tb.BridgeError("invalid_control_request")
    return req


def _rename(src: Path, dst: Path) -> None:
    tb.atomic_rename_excl(src, dst, private_lock=True)
    if src.parent != dst.parent:
        tb.fsync_dir(src.parent)


def _publish(root: Path, rid: str) -> None:
    # A staged envelope is written before its key. A crash before key creation cannot
    # perform AX, and a crash after key creation can only publish the original envelope.
    staged = _path(root, "inbox", "." + rid)
    try:
        _read_request(root, staged, rid)
    except FileNotFoundError:
        return
    _rename(staged, _path(root, "inbox", rid))


def _pending(rid: str) -> dict[str, Any]:
    return {"ok": False, "completed": False, "requestId": rid, "status": "pending"}


def _read_done(root: Path, rid: str) -> dict[str, Any] | None:
    try:
        done = _read(_path(root, "done", rid), MAX_RESULT_BYTES)
    except FileNotFoundError:
        return None
    if (not isinstance(done, dict) or done.get("requestId") != rid or done.get("status") not in TERMINAL
            or done.get("kind") not in KINDS or not isinstance(done.get("keyHash"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", done["keyHash"])
            or (done["status"] == "ok" and not isinstance(done.get("result"), dict))
            or (done["status"] != "ok" and not isinstance(done.get("error"), str))):
        raise tb.BridgeError("invalid_control_request")
    return done


def _status(root: Path, rid: str) -> dict[str, Any]:
    done = _read_done(root, rid)
    if done is not None:
        return {**done, "ok": done["status"] == "ok", "completed": True}
    for state in ("inbox", "in_flight"):
        try:
            _read_request(root, _path(root, state, rid), rid, in_flight=state == "in_flight")
            return _pending(rid)
        except FileNotFoundError:
            continue
    return {**_pending(rid), "completed": True, "status": "unconfirmed", "error": "control_request_unconfirmed"}


def submit(
    bridge: tb.Bridge, cfg: dict[str, Any], kind: str, params: dict[str, Any], *,
    via: str, scope: str | None = None, idempotency_key: str | None = None,
) -> dict[str, Any]:
    root = bridge.root / "control"
    key = uuid.uuid4().hex if idempotency_key is None else idempotency_key
    if not isinstance(key, str) or not tb.IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise tb.BridgeError("invalid_idempotency_key")
    new_id = uuid.uuid4().hex
    request = _validate_request({
        "schemaVersion": 1, "requestId": new_id, "kind": kind, "idempotencyKey": key,
        "source": {"via": via, "scope": scope}, "createdAt": tb.now_iso(), "params": params,
    }, new_id)
    key_path = _key_path(root, scope, key)
    with tb.file_lock(root / ".queue.lock", private=True):
        previous = _read_key(key_path)
        if previous is not None:
            rid = previous["requestId"]
            if previous.get("retired"):
                done = _read_done(root, rid)
                if done is not None and done["status"] != "failed":
                    return {**done, "ok": done["status"] == "ok", "completed": True}
                if done is None and previous["retired"] != "failed":
                    return {"ok": False, "completed": True, "requestId": rid, "status": "expired",
                            "kind": previous["kind"], "terminalStatus": previous["retired"],
                            "error": "control_request_unconfirmed"}
            else:
                _publish(root, rid)
                status = _status(root, rid)
                if status["status"] != "failed":
                    return status
            # A confirmed failure can be retried with this key. Keep the old
            # receipt queryable by requestId.
        if not daemon_available(bridge, cfg):
            raise tb.BridgeError("control_plane_unavailable")
        try:
            encoded = tb.encode_json_bytes(request)
        except (UnicodeError, ValueError):
            raise tb.BridgeError("invalid_control_request") from None
        if len(encoded) > MAX_REQUEST_BYTES:
            raise tb.BridgeError("file_too_large")
        tb.atomic_json_bytes(_path(root, "inbox", "." + new_id), encoded)
        tb.atomic_json(key_path, {"requestId": new_id, "kind": kind})
        _publish(root, new_id)
        return _pending(new_id)


def wait_for_request(
    bridge: tb.Bridge, rid: str, timeout: float, stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    if not isinstance(rid, str) or not tb.PAIR_UID_RE.fullmatch(rid) or not 0 <= timeout < float("inf"):
        raise tb.BridgeError("invalid_control_request")
    root = bridge.root / "control"
    deadline = time.monotonic() + timeout
    while True:
        with tb.file_lock(root / ".queue.lock", private=True):
            status = _status(root, rid)
        remaining = deadline - time.monotonic()
        if status["completed"] or remaining <= 0 or bridge.stop or (stop is not None and stop()):
            return status
        time.sleep(min(0.2, remaining))


def _pid(bridge: tb.Bridge, probes: dict[str, Any]) -> tuple[Any, int]:
    if not probes.get("axTrusted"):
        raise tb.BridgeError("ax_not_trusted")
    if probes.get("screenLocked"):
        raise tb.BridgeError("screen_locked")
    pid = probes.get("geminiPid")
    if not pid:
        raise tb.BridgeError("gemini_not_running")
    drv = bridge.ax()
    if drv is None:
        raise tb.BridgeError("ax_not_trusted")
    return drv, pid


def list_chats_with_ax(bridge: tb.Bridge, probes: dict[str, Any]) -> dict[str, Any]:
    drv, pid = _pid(bridge, probes)
    with tb.file_lock(bridge.root / ".gemini_ui.lock"):
        return drv.list_conversations(pid)


def _pair_spec(
    params: dict[str, Any], source: dict[str, Any], *, check_workspace: bool = True,
) -> dict[str, Any]:
    if set(params) - {"pairId", "label", "geminiTitle", "adapter", "t3ThreadId",
                      "replyTo", "workspace", "initPrompt", "waitSec", "replyMode"}:
        raise tb.BridgeError("invalid_control_request")
    pair_id, workspace = params.get("pairId"), params.get("workspace")
    if not isinstance(pair_id, str) or not tb.PAIR_ID_RE.fullmatch(pair_id):
        raise tb.BridgeError("invalid_pair_id")
    if workspace is not None:
        if not isinstance(workspace, str) or "\x00" in workspace or not Path(workspace).is_absolute():
            raise tb.BridgeError("invalid_control_request:workspace")
        if check_workspace:
            try:
                valid = Path(workspace).is_dir()
            except OSError:
                valid = False
            if not valid:
                raise tb.BridgeError("invalid_control_request:workspace")
    if source["via"] == "mcp" and params.get("adapter") != "local":
        raise tb.BridgeError("invalid_pair_adapter")
    if params.get("replyTo") is not None and not isinstance(params["replyTo"], str):
        raise tb.BridgeError("invalid_reply_thread_id")
    adapter, t3_thread, reply_to = tb._resolve_pair_adapter_args(
        params.get("adapter"), params.get("t3ThreadId"), params.get("replyTo"),
    )
    for field in ("label", "initPrompt"):
        val = params.get(field)
        if val is not None and (not isinstance(val, str) or "\x00" in val):
            raise tb.BridgeError("invalid_control_request")
    wait = params.get("waitSec")
    if isinstance(wait, bool) or not isinstance(wait, (float, int)) or not 0 < wait <= 300:
        raise tb.BridgeError("invalid_control_request:waitSec")
    mode = params.get("replyMode")
    if mode not in ("auto", "outbox", "ax_capture"):
        raise tb.BridgeError("invalid_pair_reply_mode")
    return {**params, "pairId": pair_id, "geminiTitle": normalized_title(params.get("geminiTitle")),
            "adapter": adapter, "t3ThreadId": t3_thread, "replyTo": reply_to}


def _check_binding(bridge: tb.Bridge, pairs: dict[str, Any], spec: dict[str, Any], source: dict[str, Any]) -> None:
    pair_id, title, scope = spec["pairId"], spec["geminiTitle"], source["scope"]
    if source["via"] == "mcp" and pair_id in pairs:
        # MCP creation is never an overwrite, including for disabled or allow-listed pairs.
        raise tb.BridgeError("pair_not_accessible_in_scope")
    if tb._has_active_tasks_for_pair(bridge, pair_id):
        raise tb.BridgeError("pair_has_active_task")
    enabled = [p for p in pairs.values() if p.get("enabled", True)]
    if scope is not None and sum(p.get("ownerScope") == scope for p in enabled) >= PAIR_LIMIT:
        raise tb.BridgeError("pair_limit_reached")
    for pair in enabled:
        if pair["pairId"] == pair_id:
            continue
        if spec["adapter"] == "t3" and tb.pair_adapter(pair) == "t3" and pair.get("t3ThreadId") == spec["t3ThreadId"]:
            raise tb.BridgeError("duplicate_t3_thread_in_pairs")
        if pair["geminiTitle"].strip() == title:
            raise tb.BridgeError("duplicate_gemini_title_in_pairs")


def _bind(
    pairs: dict[str, Any], spec: dict[str, Any], source: dict[str, Any], title: str, rid: str | None,
) -> dict[str, Any]:
    pair_id, t3_thread, reply_to = spec["pairId"], spec["t3ThreadId"], spec["replyTo"]
    previous = pairs.get(pair_id, {})
    pair = {
        "pairId": pair_id, "pairUid": uuid.uuid4().hex, "adapter": spec["adapter"],
        "label": spec.get("label") or pair_id, "t3ThreadId": t3_thread, "defaultReplyThreadId": reply_to,
        "allowedReplyThreadIds": sorted({t3_thread, reply_to}) if spec["adapter"] == "t3" else [],
        "geminiTitle": title, "workspacePath": spec.get("workspace"), "route": "gemini",
        "replyMode": spec["replyMode"], "enabled": True, "ownerScope": source["scope"],
        "createdVia": source["via"], "createdAt": previous.get("createdAt") or tb.now_iso(), "updatedAt": tb.now_iso(),
    }
    if rid is not None:
        pair["controlRequestId"] = rid
    pairs[pair_id] = pair
    return pair


def create_pair_with_ax(
    bridge: tb.Bridge, cfg: dict[str, Any], params: dict[str, Any], probes: dict[str, Any],
    source: dict[str, Any], *, request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec = _pair_spec(params, source)
    drv, pid = _pid(bridge, probes)
    pair_id, title, adapter = spec["pairId"], spec["geminiTitle"], spec["adapter"]
    rid = request["requestId"] if request is not None else None
    # Lock ordering: UI -> pairs -> ledger (short active-task check). Holding pairs_tx
    # across create prevents a send/rebind from making a valid preflight stale mid-AX.
    with tb.file_lock(bridge.root / ".gemini_ui.lock"), bridge.pairs_tx() as data:
        if rid is None and adapter == "local":
            bridge.require_v21_runtime_for_root(cfg)
        _check_binding(bridge, data["pairs"], spec, source)
        pair = _bind(data["pairs"], spec, source, title, rid)
        if len(tb.encode_json_bytes(data)) > MAX_PAIRS_BYTES:
            raise tb.BridgeError("file_too_large")
        if request is not None:
            before = drv.list_conversations(pid)
            if any(item.get("title") == title for item in before["conversations"]):
                raise tb.BridgeError("gemini_init_failed:title_already_exists")
        prompt = spec.get("initPrompt") or (
            f"This is a dedicated 1-to-1 bridge task conversation (pairId={pair_id}, adapter={adapter}). "
            f"Do not modify any files. Reply only with: READY ({title})"
        )
        wait_s = float(spec["waitSec"])
        if rid is not None:
            until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=wait_s + 60)
            bridge.write_health(probes, cfg, busy_until=until.isoformat())
        try:
            ok, actual_title, mode_detail = drv.create_task_conversation(
                pid=pid, target_title=title, init_prompt=prompt, workspace_path=spec.get("workspace"), wait_s=wait_s,
            )
        finally:
            if rid is not None:
                bridge.write_health(probes, cfg)
        if not ok:
            raise tb.BridgeError(f"gemini_init_failed:{actual_title}")
        pair["geminiTitle"] = normalized_title(actual_title)
        if len(tb.encode_json_bytes(data)) > MAX_PAIRS_BYTES:
            raise tb.BridgeError("file_too_large")
    return {"titleMode": mode_detail, "pair": pair}


def _finish(root: Path, path: Path, rid: str, req: dict[str, Any], status: str, **payload: Any) -> None:
    done = {"requestId": rid, "kind": req["kind"], "status": status, **payload,
            "keyHash": _key_path(root, req["source"]["scope"], req["idempotencyKey"]).name}
    try:
        encoded = tb.encode_json_bytes(done)
    except (UnicodeError, ValueError):
        raise tb.BridgeError("invalid_control_request") from None
    if len(encoded) > MAX_RESULT_BYTES:
        done.pop("result", None)
        done.update(status="failed", error="file_too_large")
        encoded = tb.encode_json_bytes(done)
    with tb.file_lock(root / ".queue.lock", private=True):
        tb.atomic_json_bytes(_path(root, "done", rid), encoded)
        path.unlink()
        tb.fsync_dir(path.parent)


def _recover_pair(bridge: tb.Bridge, req: dict[str, Any]) -> dict[str, Any] | None:
    pid = req["params"].get("pairId")
    pair = bridge.pairs()["pairs"].get(pid) if isinstance(pid, str) else None
    if pair is not None and pair.get("controlRequestId") == req["requestId"]:
        return {"pair": pair, "recovered": True}
    return None


def _prune(root: Path) -> None:
    cutoff = time.time() - RETENTION_SECONDS
    with tb.file_lock(root / ".queue.lock", private=True):
        done = []
        for path in (root / "done").glob("*.json"):
            if tb.PAIR_UID_RE.fullmatch(path.stem):
                done.append((path.lstat().st_mtime, path))
        removed = False
        for index, (mtime, path) in enumerate(sorted(done, reverse=True)):
            if index < MAX_RESULTS and mtime >= cutoff:
                continue
            try:
                receipt = _read_done(root, path.stem)
                if receipt is None:
                    continue
                key_path = root / "keys" / receipt["keyHash"]
                key = _read_key(key_path)
                if key and key["requestId"] == path.stem:
                    tb.atomic_json(key_path, {**key, "retired": receipt["status"]})
            except (tb.BridgeError, OSError):
                # Don't delete a receipt unless its key was safely retired.
                continue
            path.unlink()
            removed = True
        if removed:
            tb.fsync_dir(root / "done")


def _discard(bridge: tb.Bridge, path: Path, reason: str) -> None:
    bridge.log("control_request_rejected", reason=reason)
    try:
        path.unlink()  # Unlink only the queue entry, never a link target.
    except (FileNotFoundError, IsADirectoryError):
        pass


def process_control(bridge: tb.Bridge, cfg: dict[str, Any], probes: dict[str, Any], *, recover: bool = False) -> None:
    root = bridge.root / "control"
    # A durable reservation may survive a client crash before publishing its inbox file.
    with tb.file_lock(root / ".queue.lock", private=True):
        for path in (root / "inbox").glob(".*.json"):
            rid = path.stem[1:]
            if not tb.PAIR_UID_RE.fullmatch(rid):
                continue
            try:
                _publish(root, rid)
            except (tb.BridgeError, OSError) as exc:
                if path.lstat().st_mtime < time.time() - RETENTION_SECONDS:
                    _discard(bridge, path, str(exc))

    if recover:
        for path in sorted((root / "in_flight").glob("*.json")):
            rid = path.stem
            if not tb.PAIR_UID_RE.fullmatch(rid):
                continue
            with tb.file_lock(root / ".queue.lock", private=True):
                try:
                    if _read_done(root, rid) is not None:
                        path.unlink()
                        tb.fsync_dir(path.parent)
                        continue
                    req = _read_request(root, path, rid, in_flight=True)
                    if req["kind"] == "list_chats":
                        _rename(path, _path(root, "inbox", rid))
                        continue
                except (tb.BridgeError, OSError) as exc:
                    _discard(bridge, path, str(exc))
                    continue
            result = _recover_pair(bridge, req)
            if result is None:
                target = {k: req["params"].get(k) for k in ("pairId", "geminiTitle")}
                _finish(root, path, rid, req, "unconfirmed", error="control_request_unconfirmed", result=target)
            else:
                _finish(root, path, rid, req, "ok", result=result)

    created = False
    with tb.file_lock(root / ".queue.lock", private=True):
        queue = sorted((root / "inbox").glob("*.json"), key=lambda p: (p.lstat().st_mtime_ns, p.name))
    for queued in queue:
        rid = queued.stem
        if not tb.PAIR_UID_RE.fullmatch(rid):
            continue
        with tb.file_lock(root / ".queue.lock", private=True):
            try:
                req = _read_request(root, queued, rid)
                if _read_done(root, rid) is not None:
                    queued.unlink()
                    continue
                if req["kind"] == "create_pair" and created:
                    continue
                path = _path(root, "in_flight", rid)
                _rename(queued, path)
            except FileNotFoundError:
                continue
            except (tb.BridgeError, OSError) as exc:
                _discard(bridge, queued, str(exc))
                continue
            # Fail closed if the queue entry was swapped between validation and claim.
            try:
                if _read_request(root, path, rid) != req:
                    raise tb.BridgeError("invalid_control_request")
            except (tb.BridgeError, OSError) as exc:
                _discard(bridge, path, str(exc))
                continue
        try:
            if req["kind"] == "create_pair":
                created = True
                result = create_pair_with_ax(bridge, cfg, req["params"], probes, req["source"], request=req)
            else:
                result = list_chats_with_ax(bridge, probes)
        except tb.BridgeError as exc:
            _finish(root, path, rid, req, "failed", error=str(exc))
        else:
            _finish(root, path, rid, req, "ok", result=result)
    _prune(root)


def _cli_result(status: dict[str, Any], *, no_wait: bool = False, flatten: bool = False) -> int:
    out = {k: v for k, v in status.items() if k != "keyHash"}
    if flatten and out["ok"] and isinstance(out.get("result"), dict):
        out.update(out.pop("result"))
    if no_wait and not out["completed"]:
        out["ok"] = True
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["ok"] else 2 if out["completed"] else 3


def run_cli(bridge: tb.Bridge, cfg: dict[str, Any], args: Any) -> int:
    if args.pair_cmd == "request":
        return _cli_result(wait_for_request(bridge, args.request_id, args.wait))

    kind = "create_pair" if args.pair_cmd == "init" else "list_chats"
    params: dict[str, Any] = {}
    source = {"scope": None, "via": "cli"}
    if kind == "create_pair":
        params = {
            "pairId": args.id, "label": args.label, "geminiTitle": args.gemini_title,
            "adapter": getattr(args, "adapter", None), "t3ThreadId": getattr(args, "t3_thread", None),
            "replyTo": getattr(args, "reply_to", None), "replyMode": args.reply_mode,
            "workspace": str(Path(args.workspace).expanduser().resolve()) if args.workspace else None,
            "initPrompt": args.init_prompt, "waitSec": args.wait_sec,
        }
        params = _pair_spec(params, source, check_workspace=False)

    key = getattr(args, "idempotency_key", None)
    no_wait = getattr(args, "no_wait", False)
    direct = getattr(args, "direct", False)
    if direct and (key is not None or no_wait):
        raise tb.BridgeError("control_plane_unavailable")
    if not direct:
        try:
            status = submit(bridge, cfg, kind, params, via="cli", idempotency_key=key)
        except tb.BridgeError as exc:
            if str(exc) != "control_plane_unavailable" or key is not None or no_wait:
                raise
        else:
            if not no_wait and not status["completed"]:
                timeout = params["waitSec"] + 60 if kind == "create_pair" else 30
                status = wait_for_request(bridge, status["requestId"], timeout)
            return _cli_result(status, no_wait=no_wait, flatten=True)

    probes = bridge.system_probes()
    result = ({"ok": True, **create_pair_with_ax(bridge, cfg, params, probes, source)}
              if kind == "create_pair" else list_chats_with_ax(bridge, probes))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
