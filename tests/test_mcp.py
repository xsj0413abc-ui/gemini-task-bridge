#!/usr/bin/python3
"""Offline subprocess end-to-end tests for zero-dependency stdlib stdio MCP server."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills/gemini-task-bridge/scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import gemini_task_bridge as tb  # noqa: E402
from test_bridge import OFFLINE_ENV


class McpClient:
    """Line-buffered stdio JSON-RPC 2.0 test client asserting strict stdout framing."""

    def __init__(self, root: Path, args: list[str] | None = None, env_extra: dict[str, str] | None = None) -> None:
        env = {**os.environ, **OFFLINE_ENV, "HOME": str(root.parent)}
        env["T3_GEMINI_BRIDGE_ROOT"] = str(root)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if env_extra:
            env.update(env_extra)
        cmd = [sys.executable, "-B", str(SCRIPTS_DIR / "gemini_task_bridge.py"), "--root", str(root), "mcp"] + list(args or [])
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self.frames: queue.Queue = queue.Queue()
        self.raw_bad_lines: list[bytes] = []
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                break
            try:
                obj = json.loads(line.decode("utf-8"))
                self.frames.put(obj)
            except Exception:
                self.raw_bad_lines.append(line)

    def send(self, obj: object) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def send_raw(self, raw_bytes: bytes) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(raw_bytes)
        self.proc.stdin.flush()

    def recv(self, timeout: float = 5.0) -> object:
        return self.frames.get(timeout=timeout)

    def close(self, timeout: float = 5.0) -> tuple[int, bytes]:
        if self.proc.stdin:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        rc = self.proc.wait(timeout=timeout)
        self._reader.join(timeout=2.0)
        stderr_data = b""
        try:
            if self.proc.stderr:
                stderr_data = self.proc.stderr.read()
        finally:
            for stream in (self.proc.stdout, self.proc.stderr):
                if stream:
                    try:
                        stream.close()
                    except OSError:
                        pass
        return rc, stderr_data


class McpServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="t3_gem_mcp_")).resolve()
        self.root = self.tmp / "bridge_root"
        self.la_dir = self.tmp / "launchagents"
        self.la_dir.mkdir(parents=True)
        self.base_env = {"T3_GEMINI_BRIDGE_LAUNCHAGENT_DIR": str(self.la_dir)}
        self.bridge = tb.Bridge(root=self.root, t3_home=str(self.tmp / "t3_home"))
        self.bridge.setup()
        cfg = self.bridge.config()
        cfg["notifications"] = False
        self.bridge.save_config(cfg)

        # Seed one local pair (loc_a), one second local pair (loc_b), and one t3 pair (prod_t3)
        with self.bridge.pairs_tx() as pdata:
            pdata["pairs"]["loc_a"] = {
                "pairId": "loc_a", "pairUid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "adapter": "local", "label": "Local A", "t3ThreadId": None,
                "defaultReplyThreadId": None, "allowedReplyThreadIds": [],
                "geminiTitle": "Gem-Local-A", "route": "gemini", "replyMode": "auto", "enabled": True,
            }
            pdata["pairs"]["loc_b"] = {
                "pairId": "loc_b", "pairUid": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "adapter": "local", "label": "Local B", "t3ThreadId": None,
                "defaultReplyThreadId": None, "allowedReplyThreadIds": [],
                "geminiTitle": "Gem-Local-B", "route": "gemini", "replyMode": "auto", "enabled": True,
            }
            pdata["pairs"]["prod_t3"] = {
                "pairId": "prod_t3", "pairUid": "cccccccccccccccccccccccccccccccc",
                "adapter": "t3", "label": "Prod T3", "t3ThreadId": "thread-prod",
                "defaultReplyThreadId": "thread-prod", "allowedReplyThreadIds": ["thread-prod"],
                "geminiTitle": "Gem-Prod-T3", "route": "gemini", "replyMode": "auto", "enabled": True,
            }

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_01_protocol_versions_batch_rules_and_error_codes(self) -> None:
        # Test 2025-06-18: pre-init error, structuredContent, rejects batch
        c1 = McpClient(self.root, ["--allow-pairs", "loc_a"], self.base_env)
        try:
            c1.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            r_pre = c1.recv()
            self.assertEqual(r_pre["error"]["code"], -32002)

            c1.send({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
            r_init = c1.recv()
            self.assertEqual(r_init["result"]["protocolVersion"], "2025-06-18")
            self.assertIn("tools", r_init["result"]["capabilities"])
            c1.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

            # Batch MUST be rejected on 2025-06-18
            c1.send([{"jsonrpc": "2.0", "id": 3, "method": "ping"}])
            r_batch = c1.recv()
            self.assertEqual(r_batch["error"]["code"], -32600)

            # Unknown method -> -32601; unknown tool / bad bool-as-number / extra arg -> -32602
            c1.send({"jsonrpc": "2.0", "id": 4, "method": "unknown/method"})
            self.assertEqual(c1.recv()["error"]["code"], -32601)

            c1.send({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "no_such_tool", "arguments": {}}})
            self.assertEqual(c1.recv()["error"]["code"], -32602)

            c1.send({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "gemini_wait", "arguments": {"task_id": "x", "timeout_seconds": True}}})
            self.assertEqual(c1.recv()["error"]["code"], -32602)

            c1.send({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "gemini_pairs", "arguments": {"include_gemini_chats": True}}})
            self.assertEqual(c1.recv()["error"]["code"], -32602)

            # Oversized frame > 512 KiB -> -32600
            c1.send_raw(b'{"jsonrpc":"2.0","id":8,"method":"ping","pad":"' + (b"x" * (520 * 1024)) + b'"}\n')
            self.assertEqual(c1.recv()["error"]["code"], -32600)

            # Valid tool returns both content[0].text AND structuredContent on 2025-06-18
            c1.send({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "gemini_pairs", "arguments": {}}})
            r_pairs = c1.recv()["result"]
            self.assertFalse(r_pairs["isError"])
            self.assertEqual([p["pairId"] for p in r_pairs["structuredContent"]["pairs"]], ["loc_a"])
            self.assertEqual(c1.raw_bad_lines, [])
        finally:
            rc, _ = c1.close()
            self.assertEqual(rc, 0)

        # Test 2025-03-26: supports valid batch, omits notifications, rejects empty batch []
        c2 = McpClient(self.root, ["--allow-pairs", "loc_a"], self.base_env)
        try:
            c2.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
            self.assertEqual(c2.recv()["result"]["protocolVersion"], "2025-03-26")
            c2.send([{"jsonrpc": "2.0", "id": 2, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/initialized"}])
            r_b = c2.recv()
            self.assertIsInstance(r_b, list)
            self.assertEqual(len(r_b), 1)
            self.assertEqual(r_b[0]["id"], 2)

            c2.send([])
            self.assertEqual(c2.recv()["error"]["code"], -32600)
        finally:
            rc, _ = c2.close()
            self.assertEqual(rc, 0)

    def test_02_scope_acl_isolation_and_fail_closed_allowlist(self) -> None:
        # Empty / invalid allow-pairs must fail closed at startup (exit 2)
        for bad in ("", "   ", "loc_a,", "bad id"):
            p = subprocess.run(
                [sys.executable, "-B", str(SCRIPTS_DIR / "gemini_task_bridge.py"), "--root", str(self.root), "mcp", "--allow-pairs", bad],
                input=b"",
                capture_output=True,
                env={**os.environ, **self.base_env},
                check=False,
            )
            self.assertEqual(p.returncode, 2)

        # Seed secret tasks in loc_b, prod_t3, and unregistered receipt
        tb.atomic_json(self.root / "sent/payloads/sec-b.json", {"taskId": "sec-b", "pairId": "loc_b", "pairUid": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "message": "Secret B"})
        tb.atomic_json(self.root / "sent/sec-t3.json", {"taskId": "sec-t3", "pairId": "prod_t3", "pairUid": "cccccccccccccccccccccccccccccccc", "message": "Secret T3"})
        with self.bridge.ledger_tx() as ld:
            ld["tasks"]["sec-b"] = {"status": "sent", "adapter": "local", "pairId": "loc_b", "pairUid": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "payloadRelPath": "sent/payloads/sec-b.json", "messageSha256": tb.sha256_text("Secret B")[0], "messageBytes": 8}
            ld["tasks"]["sec-t3"] = {"status": "sent", "adapter": "t3", "pairId": "prod_t3", "pairUid": "cccccccccccccccccccccccccccccccc", "payloadRelPath": "sent/sec-t3.json", "messageSha256": tb.sha256_text("Secret T3")[0], "messageBytes": 9}
            ld["dispatches"]["attn-b"] = {"status": "needs_attention", "adapter": "local", "pairId": "loc_b", "pairUid": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}

        # Client scoped strictly to loc_a
        c = McpClient(self.root, ["--allow-pairs", "loc_a"], self.base_env)
        try:
            c.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
            c.recv()
            # Health MUST NOT leak loc_b or prod_t3 counts or attention titles
            c.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "gemini_health", "arguments": {}}})
            h = json.loads(c.recv()["result"]["content"][0]["text"])
            self.assertEqual(h["scopedPairCount"], 1)
            self.assertEqual(h["scopedNeedsAttentionPairs"], [])

            # Cannot send/wait/read tasks outside loc_a
            for tid in ("sec-b", "sec-t3"):
                c.send({"jsonrpc": "2.0", "id": f"r-{tid}", "method": "tools/call", "params": {"name": "gemini_read", "arguments": {"task_id": tid}}})
                res = c.recv()["result"]
                self.assertTrue(res["isError"])
                self.assertIn("task_not_accessible_in_scope", res["content"][0]["text"])
        finally:
            rc, _ = c.close()
            self.assertEqual(rc, 0)

    def test_03_eight_wait_saturation_instant_ping_and_single_cancel_vs_eof(self) -> None:
        c = McpClient(self.root, ["--allow-pairs", "loc_a"], self.base_env)
        try:
            c.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
            c.recv()

            # Send one non-blocking task via MCP
            c.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "gemini_send", "arguments": {"pair_id": "loc_a", "message": "Long task", "idempotency_key": "k1"}}})
            r_send = json.loads(c.recv()["result"]["content"][0]["text"])
            self.assertFalse(r_send["completed"])
            tid = r_send["taskId"]

            # Launch 8 concurrent long gemini_wait requests saturating all 8 wait slots
            for idx in range(100, 108):
                c.send({"jsonrpc": "2.0", "id": idx, "method": "tools/call", "params": {"name": "gemini_wait", "arguments": {"task_id": tid, "timeout_seconds": 30}}})
            time.sleep(0.25)

            # 9th wait MUST fail fast with too_many_concurrent_waits!
            c.send({"jsonrpc": "2.0", "id": 108, "method": "tools/call", "params": {"name": "gemini_wait", "arguments": {"task_id": tid, "timeout_seconds": 30}}})
            r9 = c.recv(timeout=2.0)
            self.assertEqual(r9["id"], 108)
            self.assertTrue(r9["result"]["isError"])
            self.assertIn("too_many_concurrent_waits", r9["result"]["content"][0]["text"])

            # Control lane `ping` MUST respond immediately despite 8 full wait slots!
            c.send({"jsonrpc": "2.0", "id": "ping-while-full", "method": "ping"})
            r_ping = c.recv(timeout=2.0)
            self.assertEqual(r_ping["id"], "ping-while-full")

            # Cancel ONLY wait #100 -> emits NO response for #100, keeps connection & #101..#107 alive!
            c.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 100}})
            time.sleep(0.4)

            # Complete the task on disk so remaining 7 waits finish cleanly
            reply = "MCP concurrent wait finished!"
            sha_r, len_r = tb.sha256_text(reply)
            tb.atomic_json(self.root / "sent/payloads" / f"{tid}.json", {"taskId": tid, "pairId": "loc_a", "pairUid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "message": reply})
            tb.atomic_json(self.root / "sent" / f"{tid}.receipt.json", {"taskId": tid, "accepted": True, "messageSha256": sha_r})
            with self.bridge.ledger_tx() as ld:
                ld["tasks"][tid] = {"status": "sent", "adapter": "local", "pairId": "loc_a", "pairUid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "payloadRelPath": f"sent/payloads/{tid}.json", "messageSha256": sha_r, "messageBytes": len_r}
                ld["dispatches"][tid]["status"] = "completed"

            seen_ids = set()
            for _ in range(7):
                msg = c.recv(timeout=4.0)
                seen_ids.add(msg["id"])
                self.assertEqual(msg["result"]["structuredContent"]["message"], reply)
            self.assertEqual(seen_ids, set(range(101, 108)))
            self.assertNotIn(100, seen_ids)
        finally:
            rc, _ = c.close()
            self.assertEqual(rc, 0)

    def test_04_wait_timeout_attention_idempotency_and_recreated_pair_isolation(self) -> None:
        c = McpClient(self.root, ["--allow-pairs", "loc_a"], self.base_env)
        try:
            c.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
            c.recv()

            # 1. gemini_send(wait=True, timeout_seconds=1) on queued task -> isError=False, completed=False
            c.send({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {
                    "name": "gemini_send",
                    "arguments": {"pair_id": "loc_a", "message": "Wait timeout", "wait": True, "timeout_seconds": 1, "idempotency_key": "idem-1"},
                },
            })
            r_to = c.recv(timeout=4.0)["result"]
            self.assertFalse(r_to["isError"])
            sc_to = r_to["structuredContent"]
            self.assertFalse(sc_to["completed"])
            self.assertEqual(sc_to["status"], "queued")
            tid = sc_to["taskId"]

            # 2. Idempotency replay returns same taskId with idempotentReplay=True; conflicting message -> isError=True
            c.send({
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {
                    "name": "gemini_send",
                    "arguments": {"pair_id": "loc_a", "message": "Wait timeout", "idempotency_key": "idem-1"},
                },
            })
            r_rep = c.recv(timeout=2.0)["result"]
            self.assertFalse(r_rep["isError"])
            self.assertEqual(r_rep["structuredContent"]["taskId"], tid)
            self.assertTrue(r_rep["structuredContent"]["idempotentReplay"])

            c.send({
                "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {
                    "name": "gemini_send",
                    "arguments": {"pair_id": "loc_a", "message": "Different body", "idempotency_key": "idem-1"},
                },
            })
            r_conf = c.recv(timeout=2.0)["result"]
            self.assertTrue(r_conf["isError"])
            self.assertIn("idempotency_key_conflict", r_conf["content"][0]["text"])

            # 3. needs_attention returns isError=False, completed=False, attentionEpisode
            with self.bridge.ledger_tx() as ld:
                ld["dispatches"][tid]["status"] = "needs_attention"
                ld["dispatches"][tid]["attentionEpisode"] = 2
            c.send({
                "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "gemini_wait", "arguments": {"task_id": tid, "timeout_seconds": 1, "return_on_attention": True}},
            })
            r_att = c.recv(timeout=3.0)["result"]
            self.assertFalse(r_att["isError"])
            sc_att = r_att["structuredContent"]
            self.assertFalse(sc_att["completed"])
            self.assertEqual(sc_att["status"], "needs_attention")
            self.assertEqual(sc_att["attentionEpisode"], 2)

            # 4. Complete task under old pairUid, then recreate loc_a with a new pairUid -> gemini_read denied!
            old_msg = "Old generation secret"
            sha_o, len_o = tb.sha256_text(old_msg)
            tb.atomic_json(self.root / "sent/payloads" / f"{tid}.json", {
                "taskId": tid, "pairId": "loc_a", "pairUid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "message": old_msg,
            })
            with self.bridge.ledger_tx() as ld:
                ld["tasks"][tid] = {
                    "status": "sent", "adapter": "local", "pairId": "loc_a",
                    "pairUid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "payloadRelPath": f"sent/payloads/{tid}.json",
                    "messageSha256": sha_o, "messageBytes": len_o,
                }
                ld["dispatches"][tid]["status"] = "completed"

            c.send({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "gemini_read", "arguments": {"task_id": tid}}})
            r_ok = c.recv(timeout=2.0)["result"]
            self.assertFalse(r_ok["isError"])
            self.assertEqual(r_ok["structuredContent"]["message"], old_msg)

            with self.bridge.pairs_tx() as pdata:
                pdata["pairs"]["loc_a"]["pairUid"] = "dddddddddddddddddddddddddddddddd"

            c.send({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "gemini_read", "arguments": {"task_id": tid}}})
            r_denied = c.recv(timeout=2.0)["result"]
            self.assertTrue(r_denied["isError"])
            self.assertIn("task_not_accessible_in_scope", r_denied["content"][0]["text"])
        finally:
            rc, _ = c.close()
            self.assertEqual(rc, 0)

    def test_05_batch_2025_03_26_async_ping_cancel_and_eof(self) -> None:
        c = McpClient(self.root, ["--allow-pairs", "loc_a"], self.base_env)
        try:
            c.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
            self.assertEqual(c.recv()["result"]["protocolVersion"], "2025-03-26")

            c.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "gemini_send", "arguments": {"pair_id": "loc_a", "message": "Async batch item"}}})
            tid = json.loads(c.recv()["result"]["content"][0]["text"])["taskId"]

            # Launch a 10s batch wait + batch ping on 2025-03-26
            t0 = time.monotonic()
            c.send([
                {"jsonrpc": "2.0", "id": "bw-1", "method": "tools/call", "params": {"name": "gemini_wait", "arguments": {"task_id": tid, "timeout_seconds": 10}}},
                {"jsonrpc": "2.0", "id": "bp-1", "method": "ping"},
            ])

            # Independent ping MUST return immediately while batch wait is running
            c.send({"jsonrpc": "2.0", "id": "solo-ping", "method": "ping"})
            r_solo = c.recv(timeout=1.0)
            self.assertEqual(r_solo["id"], "solo-ping")
            self.assertLess(time.monotonic() - t0, 0.8)

            # Cancel batch wait -> batch emits only bp-1 response promptly
            c.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "bw-1"}})
            r_batch = c.recv(timeout=2.0)
            self.assertIsInstance(r_batch, list)
            self.assertEqual(r_batch, [{"jsonrpc": "2.0", "id": "bp-1", "result": {}}])

            # Second 10s batch wait + immediate EOF shuts down cleanly without hanging 10s
            c.send([
                {"jsonrpc": "2.0", "id": "bw-2", "method": "tools/call", "params": {"name": "gemini_wait", "arguments": {"task_id": tid, "timeout_seconds": 10}}},
            ])
            time.sleep(0.15)
            t_close = time.monotonic()
            rc, _ = c.close(timeout=2.5)
            self.assertEqual(rc, 0)
            self.assertLess(time.monotonic() - t_close, 1.5)
        except Exception:
            c.close(timeout=2.0)
            raise

    def test_06_v21_gate_after_startup_and_corrupt_archive_error(self) -> None:
        # 1. Corrupted archived payload MUST surface error via MCP gemini_wait
        ctid = "mcp-corrupt-1"
        tb.atomic_json(self.root / "sent/payloads" / f"{ctid}.json", {
            "taskId": ctid, "pairId": "loc_a", "pairUid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "message": "TAMPERED",
        })
        with self.bridge.ledger_tx() as ld:
            ld["tasks"][ctid] = {
                "status": "sent", "adapter": "local", "pairId": "loc_a",
                "pairUid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "payloadRelPath": f"sent/payloads/{ctid}.json",
                "messageSha256": "0" * 64, "messageBytes": 10,
            }
            ld["dispatches"][ctid] = {
                "status": "completed", "adapter": "local", "pairId": "loc_a",
                "pairUid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            }

        # Start MCP client scoped to all adapters (so prod_t3 is allowed) while root is clean v2.1 offline
        c = McpClient(self.root, ["--adapter-filter", "all", "--allow-pairs", "loc_a,prod_t3"], self.base_env)
        try:
            c.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
            c.recv()

            c.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "gemini_wait", "arguments": {"task_id": ctid, "timeout_seconds": 1}}})
            r_cor = c.recv(timeout=2.0)["result"]
            self.assertTrue(r_cor["isError"])
            self.assertIn("archived_payload_corrupted", r_cor["content"][0]["text"])

            # Simulate an old v2.0 daemon taking .process.lock after MCP startup -> gemini_send must NOT slip into v2 compat!
            tb.atomic_json(self.root / ".daemon_health.json", {
                "schemaVersion": 1, "pid": 42424, "startedAt": tb.now_iso(), "lastScanAt": tb.now_iso(),
                "axTrusted": True, "screenLocked": False, "geminiRunning": True, "geminiPid": 99999,
                "globalMode": "hybrid", "lastError": None,
            })
            with tb.file_lock(self.root / ".process.lock", nonblocking=True) as fd:
                os.ftruncate(fd, 0)
                os.write(fd, b"42424")
                c.send({
                    "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "gemini_send", "arguments": {"pair_id": "prod_t3", "message": "Must reject legacy compat"}},
                })
                r_gate = c.recv(timeout=2.0)["result"]
                self.assertTrue(r_gate["isError"])
                self.assertIn("daemon_upgrade_required_for_v21_features", r_gate["content"][0]["text"])

            self.assertEqual(list((self.root / "inbox").glob("*.task.json")), [])
        finally:
            rc, _ = c.close()
            self.assertEqual(rc, 0)

    def test_07_legacy_t3_none_uid_binding_preserved(self) -> None:
        import io
        from unittest import mock
        import mcp_server  # noqa: E402

        with self.bridge.pairs_tx() as pdata:
            pdata["pairs"]["leg_t3"] = {
                "pairId": "leg_t3", "adapter": "t3", "label": "Legacy T3",
                "t3ThreadId": "thread-orig", "defaultReplyThreadId": "thread-orig",
                "allowedReplyThreadIds": ["thread-orig"],
                "geminiTitle": "Gem-Same-Title", "route": "gemini", "replyMode": "auto", "enabled": True,
            }

        # 1. Rebind t3ThreadId/defaultReplyThreadId on same legacy pairId/adapter/geminiTitle/pairUid=None -> rejected in lock!
        scope = mcp_server.McpScope(adapter_filter="all", allow_pairs={"leg_t3"})
        srv = mcp_server.McpStdioServer(self.bridge, scope, io.BytesIO(), io.BytesIO())
        try:
            orig_pairs = self.bridge.pairs
            swapped = [False]

            def hook_pairs() -> dict:
                data = orig_pairs()
                if not swapped[0]:
                    # Call #1 in _tool_send snapshot leaves original; mutate right before lock read in submit_task
                    swapped[0] = True
                    return data
                data["pairs"]["leg_t3"]["t3ThreadId"] = "thread-evil"
                data["pairs"]["leg_t3"]["defaultReplyThreadId"] = "thread-evil"
                data["pairs"]["leg_t3"]["allowedReplyThreadIds"] = ["thread-evil"]
                tb.atomic_json(self.root / "pairs.json", data)
                return orig_pairs()

            with mock.patch.object(self.bridge, "pairs", side_effect=hook_pairs):
                is_err, payload = srv._tool_send({"pair_id": "leg_t3", "message": "Hijacked?"}, threading.Event())
            self.assertTrue(is_err)
            self.assertEqual(payload["error"], "pair_not_accessible_in_scope")
            self.assertEqual(list((self.root / "inbox").glob("*.task.json")), [])
            self.assertIsNone(self.bridge.pairs()["pairs"]["leg_t3"].get("pairUid"))
        finally:
            srv.fast_pool.shutdown(wait=True)
            srv.wait_pool.shutdown(wait=True)

        # 2. Unchanged legacy T3 pair via real stdio MCP subprocess Succeeds and mints pairUid cleanly
        c = McpClient(self.root, ["--adapter-filter", "all", "--allow-pairs", "leg_t3"], self.base_env)
        try:
            c.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
            c.recv()
            c.send({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "gemini_send", "arguments": {"pair_id": "leg_t3", "message": "Unchanged legacy"}},
            })
            r_ok = c.recv(timeout=2.0)["result"]
            self.assertFalse(r_ok["isError"])
            sc = r_ok["structuredContent"]
            self.assertEqual(sc["pairId"], "leg_t3")
            uid = self.bridge.pairs()["pairs"]["leg_t3"].get("pairUid")
            self.assertIsNotNone(uid)
        finally:
            rc, _ = c.close()
            self.assertEqual(rc, 0)

    def test_08_small_max_inbox_bytes_reads_and_waits_large_reply(self) -> None:
        cfg = self.bridge.config()
        cfg["max_inbox_bytes"] = 512
        self.bridge.save_config(cfg)

        c = McpClient(self.root, ["--allow-pairs", "loc_a"], self.base_env)
        try:
            c.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
            c.recv()

            c.send({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "gemini_send", "arguments": {"pair_id": "loc_a", "message": "Short req"}},
            })
            r_send = c.recv(timeout=2.0)["result"]
            self.assertFalse(r_send["isError"])
            tid = r_send["structuredContent"]["taskId"]

            with self.bridge.ledger_tx() as ld:
                ld["dispatches"][tid]["status"] = "in_flight"
            big_reply = "R" * 700
            tb.atomic_json(self.root / "outbox" / f"{tid}.json", {
                "taskId": tid, "pairId": "loc_a", "pairUid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "message": big_reply,
            })
            self.bridge.process_outbox(self.bridge.config())

            c.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "gemini_read", "arguments": {"task_id": tid}}})
            r_read = c.recv(timeout=2.0)["result"]
            self.assertFalse(r_read["isError"])
            self.assertEqual(r_read["structuredContent"]["message"], big_reply)

            c.send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "gemini_wait", "arguments": {"task_id": tid, "timeout_seconds": 1}}})
            r_wait = c.recv(timeout=2.0)["result"]
            self.assertFalse(r_wait["isError"])
            self.assertEqual(r_wait["structuredContent"]["message"], big_reply)
        finally:
            rc, _ = c.close()
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
