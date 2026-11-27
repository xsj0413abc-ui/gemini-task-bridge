"""Offline control-plane tests. All AX operations use the existing MockAXDriver."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import datetime
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock

from test_bridge import MockAXDriver, tb
from test_mcp import McpClient

import control_plane as cp
import mcp_server as mcp


class ControlPlaneTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="gemini_control_test_")
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.root = base / "runtime"
        self.ax = MockAXDriver()
        self.noax = MockAXDriver()
        self.noax.ax_trusted = False
        self.daemon = tb.Bridge(root=self.root, ax_driver=self.ax, t3_home=str(base / "t3"))
        self.client = tb.Bridge(root=self.root, ax_driver=self.noax, t3_home=str(base / "t3"))
        env = {
            "HOME": str(base), "T3_GEMINI_BRIDGE_LAUNCHAGENT_DIR": str(base / "LaunchAgents"),
            "T3_GEMINI_BRIDGE_MCP_SCOPE": "test", "PYTHONDONTWRITEBYTECODE": "1",
        }
        env_patch = mock.patch.dict(os.environ, env)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        os.environ.pop("GEMINI_TASK_BRIDGE_MCP_SCOPE", None)
        self.daemon.setup()
        self.cfg = self.daemon.config()
        self.cfg["notifications"] = False
        self.daemon.save_config(self.cfg)
        self._health()
        self.control = self.root / "control"

    def _health(self) -> None:
        self.daemon.write_health(self.ax.system_probes(), self.cfg)

    def _cli(self, *args: str) -> tuple[int, dict]:
        ns = tb.build_parser().parse_args(["pair", *args])
        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            rc = tb.cmd_pair(self.client, ns)
        return rc, json.loads(out.getvalue())

    def _init(self, *args: str) -> tuple[int, dict]:
        return self._cli("init", "--id", "cli-pair", "--adapter", "local", "--gemini-title", "CLI Title", *args)

    def _submit(self, pair_id="one", title=None, key="key", scope="codex", **params) -> str:
        return cp.submit(
            self.client, self.cfg, "create_pair",
            {"pairId": pair_id, "geminiTitle": title or f"Title {pair_id}", "adapter": "local",
             "waitSec": 3, "replyMode": "auto", **params},
            via="mcp", scope=scope, idempotency_key=key,
        )["requestId"]

    def _result(self, rid):
        return cp.wait_for_request(self.client, rid, 0)

    def _server(self, scope, allow_pairs=None, adapter_filter="local"):
        server = mcp.McpStdioServer(self.client, mcp.McpScope(adapter_filter, allow_pairs, scope), None, None)
        self.addCleanup(server.fast_pool.shutdown)
        self.addCleanup(server.wait_pool.shutdown)
        return server

    def _call(self, server, tool, **args):
        return server._execute_tool(tool, mcp._validate_tool_args(tool, args), threading.Event())

    def _seed_pair(self, pid, *, scope=None, enabled=True):
        pair = {
            "pairId": pid, "pairUid": tb.uuid.uuid4().hex, "adapter": "local", "route": "gemini",
            "geminiTitle": f"Title {pid}", "enabled": enabled,
        }
        if scope is not None:
            pair["ownerScope"] = scope
        with self.daemon.pairs_tx() as data:
            data["pairs"][pid] = pair
        self.ax.add_conv(pair["geminiTitle"])
        return pair

    def test_cli_untrusted_daemon_creation_and_result_lookup(self):
        with mock.patch.object(self.client, "system_probes", side_effect=AssertionError("client AX")), \
                mock.patch.object(cp, "wait_for_request", wraps=self._daemon_wait) as wait:
            rc, out = self._init("--idempotency-key", "cli", "--wait-sec", "2")
        self.assertEqual(rc, 0)
        self.assertEqual(wait.call_args.args[2], 62)
        self.assertEqual(out["pair"]["controlRequestId"], out["requestId"])
        self.assertEqual(out["pair"]["createdVia"], "cli")
        self.assertIsNone(out["pair"]["ownerScope"])
        rc, replay = self._init("--idempotency-key", "cli")
        self.assertEqual((rc, replay["requestId"]), (0, out["requestId"]))
        rc, query = self._cli("request", out["requestId"], "--wait", "0", "--json")
        self.assertEqual((rc, query["result"]["pair"]), (0, out["pair"]))
        self.assertEqual(len(self.ax.injected_prompts), 1)

    def _daemon_wait(self, bridge, rid, timeout, stop=None):
        self.daemon.scan()
        return self._result_unpatched(bridge, rid, timeout, stop)

    _result_unpatched = staticmethod(cp.wait_for_request)

    def test_cli_no_wait_timeout_and_health_fallback(self):
        rc, pending = self._init("--no-wait", "--idempotency-key", "cli")
        self.assertEqual((rc, pending["status"], pending["completed"]), (0, "pending", False))
        rid = pending["requestId"]
        self.assertEqual(self._cli("request", rid, "--wait", "0.001"),
                         (3, {"ok": False, "completed": False, "requestId": rid, "status": "pending"}))
        health = self.daemon.read_health()
        health["lastScanAt"] = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)).isoformat()
        tb.atomic_json(self.root / ".daemon_health.json", health)
        with self.assertRaisesRegex(tb.BridgeError, "^ax_not_trusted$"):
            self._cli("gemini-chats")
        self.noax.ax_trusted = True
        rc, direct = self._cli("gemini-chats")
        self.assertEqual((rc, direct["conversations"]), (0, []))
        self._health()
        self.noax.add_conv("Only diagnostic client")
        rc, direct = self._cli("gemini-chats", "--direct")
        self.assertEqual((rc, direct["conversations"][0]["title"]), (0, "Only diagnostic client"))
        self.assertEqual(self._result(rid)["status"], "pending")

    def test_cli_t3_and_list_go_through_daemon(self):
        ws = self.root.parent / "project workspace"
        ws.mkdir()
        with mock.patch.object(cp, "wait_for_request", wraps=self._daemon_wait) as wait:
            rc, out = self._cli("init", "--id", "t3-pair", "--t3-thread", "a", "--reply-to", "b",
                                "--gemini-title", "T3 Title", "--workspace", str(ws), "--init-prompt", "Ready?")
            rc_list, listed = self._cli("gemini-chats", "--json")
        self.assertEqual((rc, rc_list), (0, 0))
        self.assertEqual(wait.call_args.args[2], 30)
        self.assertEqual(out["pair"]["defaultReplyThreadId"], "b")
        self.assertEqual(out["pair"]["workspacePath"], str(ws))
        self.assertEqual(listed["conversations"][0]["title"], "T3 Title")
        self.assertEqual(self.ax.injected_prompts, [("T3 Title", "Ready?")])

    def test_simultaneous_submit_replays_same_id_and_scope_keys_are_isolated(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda _: self._submit(), range(8)))
        self.assertEqual(len(set(ids)), 1)
        other = self._submit("two", scope="claude-code")
        self.assertNotEqual(ids[0], other)
        self.daemon.scan()
        self.assertEqual(len(self.ax.injected_prompts), 1)
        self.daemon.scan()
        self.assertEqual(len(self.ax.injected_prompts), 2)
        self.assertEqual(self._result(ids[0])["status"], "ok")
        self.assertEqual(self._result(other)["status"], "ok")

    def test_recovery_publishes_staged_once_if_client_dies_after_key(self):
        with mock.patch.object(cp, "_publish", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                self._submit()
        self.assertEqual(len(list((self.control / "inbox").glob(".*.json"))), 1)
        self.daemon.scan(recover=True)
        rid = self._submit()
        self.assertEqual(self._result(rid)["status"], "ok")
        self.assertEqual(len(self.ax.injected_prompts), 1)

    def _crash_after_ax(self, rid):
        write = tb.atomic_json

        def crash(path, data):
            if path == self.root / "pairs.json":
                raise SystemExit("simulated crash before pairs commit")
            write(path, data)
        with mock.patch.object(tb, "atomic_json", side_effect=crash):
            with self.assertRaises(SystemExit):
                self.daemon.scan()
        self.assertEqual(self._result(rid)["status"], "pending")
        self.assertEqual(self.daemon.pairs()["pairs"], {})

    def test_recovery_after_ax_before_commit_is_unconfirmed_even_with_exact_title(self):
        rid = self._submit()
        self._crash_after_ax(rid)
        restarted = tb.Bridge(root=self.root, ax_driver=self.ax)
        with mock.patch.object(self.ax, "list_conversations", side_effect=AssertionError("recovery AX")):
            restarted.scan(recover=True)
        got = self._result(rid)
        self.assertEqual(got["status"], "unconfirmed")
        self.assertEqual(got["error"], "control_request_unconfirmed")
        self.assertEqual(got["result"], {"pairId": "one", "geminiTitle": "Title one"})
        self.assertEqual(self._submit(), rid)
        self.assertEqual(self.daemon.pairs()["pairs"], {})
        self.assertEqual(len(self.ax.injected_prompts), 1)
        server = self._server("codex")
        failed, replay = self._call(server, "gemini_pair_init", title="Title one", idempotency_key="key")
        self.assertTrue(failed)
        self.assertEqual(replay["result"], got["result"])

    def test_recovery_does_not_restore_removed_pair_or_overwrite_rebinding(self):
        for rebind in (False, True):
            with self.subTest(rebind=rebind):
                pid = f"pair-{rebind}"
                rid = self._submit(pid, key=pid)
                with mock.patch.object(cp, "_finish", side_effect=SystemExit):
                    with self.assertRaises(SystemExit):
                        self.daemon.scan()
                with self.daemon.pairs_tx() as pairs:
                    previous = pairs["pairs"].pop(pid)
                    if rebind:
                        previous.pop("controlRequestId")
                        pairs["pairs"][pid] = {**previous, "pairUid": tb.uuid.uuid4().hex,
                                               "geminiTitle": f"{pid} replacement"}
                with mock.patch.object(self.ax, "list_conversations", side_effect=AssertionError("recovery AX")):
                    self.daemon.scan(recover=True)
                self.assertEqual(self._result(rid)["status"], "unconfirmed")
                pair = self.daemon.pairs()["pairs"].get(pid)
                if rebind:
                    self.assertEqual(pair["geminiTitle"], f"{pid} replacement")
                else:
                    self.assertIsNone(pair)
        self.assertEqual(len(self.ax.injected_prompts), 2)

    def test_claim_without_ax_intent_does_not_bind_preexisting_title(self):
        rid = self._submit()
        self.ax.add_conv("Title one")
        cp._rename(self.control / "inbox" / f"{rid}.json", self.control / "in_flight" / f"{rid}.json")
        self.daemon.scan(recover=True)
        self.assertEqual(self._result(rid)["status"], "unconfirmed")
        self.assertEqual(self.daemon.pairs()["pairs"], {})
        self.assertEqual(self.ax.injected_prompts, [])

    def test_recovery_from_pair_commit_and_from_done_write(self):
        rid = self._submit()
        with mock.patch.object(cp, "_finish", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                self.daemon.scan()
        pair = self.daemon.pairs()["pairs"]["one"]
        with mock.patch.object(self.ax, "list_conversations", side_effect=AssertionError("AX not needed")):
            self.daemon.scan(recover=True)
        self.assertEqual(self._result(rid)["result"]["pair"], pair)
        self.assertEqual(len(self.ax.injected_prompts), 1)

        second = self._submit("two", key="two")
        original = Path.unlink
        flight = self.control / "in_flight" / f"{second}.json"

        def crash(path, *args, **kwargs):
            if path == flight:
                raise SystemExit
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, "unlink", crash):
            with self.assertRaises(SystemExit):
                self.daemon.scan()
        receipt = self._result(second)
        self.assertTrue(receipt["ok"])
        self.daemon.scan(recover=True)
        self.assertEqual(self._result(second), receipt)
        self.assertFalse(flight.exists())

    def test_list_recovery_and_all_lists_per_scan(self):
        ids = [cp.submit(self.client, self.cfg, "list_chats", {}, via="cli")["requestId"] for _ in range(3)]
        rid = ids[0]
        cp._rename(self.control / "inbox" / f"{rid}.json", self.control / "in_flight" / f"{rid}.json")
        a = self._submit()
        b = self._submit("two", key="two")
        self.daemon.scan(recover=True)
        self.assertTrue(all(self._result(rid)["ok"] for rid in ids))
        self.assertEqual([self._result(a)["status"], self._result(b)["status"]], ["ok", "pending"])

    def test_scope_limit_only_counts_enabled_and_owned(self):
        self._seed_pair("disabled", scope="codex", enabled=False)
        self._seed_pair("foreign", scope="claude-code")
        self._seed_pair("legacy")
        for i in range(10):
            rid = self._submit(f"p{i}", key=f"key-{i}")
            self.daemon.scan()
            self.assertEqual(self._result(rid)["status"], "ok")
        rid = self._submit("eleven")
        self.daemon.scan()
        self.assertEqual(self._result(rid)["error"], "pair_limit_reached")
        self.assertEqual(len(self.ax.injected_prompts), 10)

    def test_mcp_scope_isolation_and_list_redaction(self):
        mine = self._seed_pair("mine", scope="codex")
        self._seed_pair("other", scope="claude-code")
        self._seed_pair("allow")
        self._seed_pair("legacy")
        self.ax.add_conv("Unbound")
        server = self._server("codex", {"allow"})
        with mock.patch.object(cp, "wait_for_request", wraps=self._daemon_wait), \
                mock.patch.object(self.client, "system_probes", side_effect=AssertionError("MCP must not use AX")):
            error, listed = self._call(server, "gemini_list_chats")
            self.assertFalse(error)
        chats = {item["title"]: item for item in listed["conversations"]}
        self.assertEqual(chats["Title mine"]["pairId"], "mine")
        self.assertEqual(chats["Title allow"]["pairId"], "allow")
        self.assertEqual(chats["Title other"], {"title": "Title other", "bound": True, "visible": False})
        self.assertFalse(chats["Unbound"]["bound"])
        self.assertEqual({p["pairId"] for p in self._call(server, "gemini_pairs")[1]["pairs"]}, {"mine", "allow"})
        with self.daemon.ledger_tx() as ledger:
            ledger["dispatches"]["secret"] = {"pairId": "other", "pairUid": self.daemon.pairs()["pairs"]["other"]["pairUid"],
                                               "status": "in_flight", "adapter": "local"}
        for tool in ("gemini_read", "gemini_wait"):
            self.assertEqual(self._call(server, tool, task_id="secret")[1]["error"], "task_not_accessible_in_scope")
        self.assertEqual(self._call(server, "gemini_send", pair_id="other", message="Blocked")[1]["error"],
                         "pair_not_accessible_in_scope")
        self.assertEqual(self.daemon.pairs()["pairs"]["mine"], mine)
        self.assertEqual({p["pairId"] for p in self._call(self._server(None), "gemini_pairs")[1]["pairs"]},
                         {"mine", "other", "allow", "legacy"})

    def test_mcp_pair_summary_filters_stale_uid_and_adapter_before_selecting_task(self):
        pair = self._seed_pair("mine", scope="codex")
        server = self._server("codex", adapter_filter="all")
        with self.daemon.ledger_tx() as ledger:
            base = {"pairId": "mine", "pairUid": pair["pairUid"], "status": "in_flight", "adapter": "local"}
            ledger["dispatches"].update(old={**base, "pairUid": tb.uuid.uuid4().hex},
                                        wrong_adapter={**base, "adapter": "t3"})
        _, out = self._call(server, "gemini_pairs")
        self.assertIsNone(out["pairs"][0]["inFlightTaskId"])
        with self.daemon.ledger_tx() as ledger:
            ledger["dispatches"]["valid"] = base
        _, out = self._call(server, "gemini_pairs")
        self.assertEqual(out["pairs"][0]["inFlightTaskId"], "valid")

    def test_mcp_send_rechecks_owner_under_pair_lock(self):
        self._seed_pair("mine", scope="codex")
        server = self._server("codex")
        original = self.client.submit_task

        def transfer_then_send(**kwargs):
            with self.daemon.pairs_tx() as data:
                data["pairs"]["mine"]["ownerScope"] = "claude-code"
            return original(**kwargs)
        with mock.patch.object(self.client, "submit_task", side_effect=transfer_then_send):
            failed, out = self._call(server, "gemini_send", pair_id="mine", message="Do not dispatch")
        self.assertTrue(failed)
        self.assertEqual(out["error"], "pair_not_accessible_in_scope")
        self.assertEqual(list((self.root / "inbox").glob("*.task.json")), [])

    def test_busy_health_extends_both_gates_only_until_deadline(self):
        rid = self._submit()
        create = self.ax.create_task_conversation
        now = datetime.datetime.now(datetime.timezone.utc)

        def busy(**kwargs):
            report = self.daemon.read_health()
            self.assertAlmostEqual((tb.parse_iso_utc(report["busyUntil"]) - now).total_seconds(), 63, delta=2)
            report["lastScanAt"] = (now - datetime.timedelta(seconds=20)).isoformat()
            tb.atomic_json(self.root / ".daemon_health.json", report)
            self.assertTrue(cp.daemon_available(self.client, self.cfg))
            self.assertEqual(self.client.check_v21_runtime_for_root(self.cfg), (True, "live_v21_daemon"))
            report["busyUntil"] = (now - datetime.timedelta(seconds=1)).isoformat()
            tb.atomic_json(self.root / ".daemon_health.json", report)
            self.assertFalse(cp.daemon_available(self.client, self.cfg))
            self.assertFalse(self.client.check_v21_runtime_for_root(self.cfg)[0])
            return create(**kwargs)

        with tb.file_lock(self.root / ".process.lock") as fd:
            os.write(fd, str(os.getpid()).encode("ascii"))
            with mock.patch.object(self.ax, "create_task_conversation", side_effect=busy):
                self.daemon.scan()
        self.assertTrue(self._result(rid)["ok"])
        self.assertNotIn("busyUntil", self.daemon.read_health())

    def test_same_key_retries_failed_attempt_and_keeps_attempt_history(self):
        server = self._server("codex")
        self.ax.gemini_running = False
        with mock.patch.object(cp, "wait_for_request", wraps=self._daemon_wait):
            error, failed = self._call(server, "gemini_pair_init", title="Retry title")
            self.ax.gemini_running = True
            error2, done = self._call(server, "gemini_pair_init", title="Retry title")
            error3, replay = self._call(server, "gemini_pair_init", title="Retry title")
        self.assertTrue(error)
        self.assertEqual(failed["error"], "gemini_not_running")
        self.assertFalse(error2 or error3)
        self.assertNotEqual(failed["requestId"], done["requestId"])
        self.assertEqual(replay["requestId"], done["requestId"])
        self.assertEqual(self._result(failed["requestId"])["status"], "failed")
        self.assertEqual(len(self.ax.injected_prompts), 1)

    def test_mcp_stdio_timeout_replay_and_argument_defaults(self):
        env = {"T3_GEMINI_BRIDGE_LAUNCHAGENT_DIR": str(self.root.parent / "no-launchagents")}
        client = McpClient(self.root, ["--scope", "claude-code"], env)
        try:
            client.send({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
            client.recv()
            client.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            tools = {spec["name"] for spec in client.recv()["result"]["tools"]}
            self.assertTrue({"gemini_list_chats", "gemini_pair_init"} <= tools)
            args = {"title": "工作台", "timeout_seconds": 1}
            method = {"jsonrpc": "2.0", "method": "tools/call",
                      "params": {"name": "gemini_pair_init", "arguments": args}}
            client.send({"id": 3, **method})
            waiting = client.recv()["result"]
            self.assertFalse(waiting["isError"])
            self.assertFalse(waiting["structuredContent"]["completed"])
            self.daemon.scan()
            client.send({"id": 4, **method})
            done = client.recv()["result"]["structuredContent"]
            self.assertEqual(done["requestId"], waiting["structuredContent"]["requestId"])
            self.assertTrue(done["completed"])
            self.assertRegex(done["pair"]["pairId"], r"^claude-code-[0-9a-f]{6}$")
            self.assertEqual(done["pair"]["ownerScope"], "claude-code")
            self.assertEqual(len(self.ax.injected_prompts), 1)
        finally:
            self.assertEqual(client.close()[0], 0)

    def test_mcp_fails_without_daemon_and_never_falls_back_to_client_ax(self):
        (self.root / ".daemon_health.json").unlink()
        server = self._server("codex")
        with mock.patch.object(self.client, "system_probes", side_effect=AssertionError("client AX")):
            for tool, params in (("gemini_pair_init", {"title": "No"}), ("gemini_list_chats", {})):
                self.assertEqual(self._call(server, tool, **params),
                                 (True, {"ok": False, "error": "control_plane_unavailable"}))
        self.assertEqual(list((self.control / "inbox").iterdir()), [])

    def test_scope_env_precedence_and_adapter_filter(self):
        with mock.patch.dict(os.environ, {"GEMINI_TASK_BRIDGE_MCP_SCOPE": "modern",
                                          "T3_GEMINI_BRIDGE_MCP_SCOPE": "legacy"}):
            self.assertEqual(mcp.McpScope.from_args(None, None).scope, "modern")
            self.assertEqual(mcp.McpScope.from_args(None, None, "explicit").scope, "explicit")
        self.assertEqual(mcp.McpScope.from_args(None, None).scope, "test")
        with self.assertRaisesRegex(tb.BridgeError, "invalid_mcp_scope"):
            mcp.McpScope.from_args(None, None, "Bad Scope")
        for server in (self._server("codex", adapter_filter="t3"), self._server(None, {"other"})):
            self.assertEqual(self._call(server, "gemini_pair_init", title="No")[1]["error"],
                             "pair_not_accessible_in_scope")

    def test_daemon_revalidates_workspace_adapter_and_active_task(self):
        self._seed_pair("legacy")
        with self.daemon.ledger_tx() as ledger:
            ledger["dispatches"]["active"] = {"pairId": "active", "status": "queued"}
        for params, expected in (
            ({"workspace": "."}, "invalid_control_request:workspace"),
            ({"workspace": str(self.root / "missing")}, "invalid_control_request:workspace"),
            ({"adapter": "t3", "t3ThreadId": "x"}, "invalid_pair_adapter"),
            ({"geminiTitle": "Title legacy"}, "duplicate_gemini_title_in_pairs"),
            ({"pairId": "active"}, "pair_has_active_task"),
        ):
            with self.subTest(params=params):
                rid = self._submit(key=tb.uuid.uuid4().hex, **params)
                self.daemon.scan()
                self.assertEqual(self._result(rid)["error"], expected)
        self.assertEqual(self.ax.injected_prompts, [])

    def test_list_and_task_dispatch_serialize_on_shared_ui_lock(self):
        self._seed_pair("target")
        self.daemon.submit_task(pair_id="target", message="actual task", task_id="queued-task")
        rid = cp.submit(self.client, self.cfg, "list_chats", {}, via="cli")["requestId"]
        entered = threading.Event()
        release = threading.Event()
        orig_list = self.ax.list_conversations

        def blocked_list(pid):
            entered.set()
            self.assertTrue(release.wait(5))
            return orig_list(pid)

        with mock.patch.object(self.ax, "list_conversations", side_effect=blocked_list), ThreadPoolExecutor(2) as pool:
            listing = pool.submit(cp.process_control, self.daemon, self.cfg, self.ax.system_probes())
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(BlockingIOError), tb.file_lock(self.root / ".gemini_ui.lock", nonblocking=True):
                    self.fail("list must hold the actual task-dispatch UI lock")
                entered_dispatch = threading.Event()

                def dispatch():
                    entered_dispatch.set()
                    self.daemon.process_inbox(self.cfg, self.ax.system_probes())
                task = pool.submit(dispatch)
                self.assertTrue(entered_dispatch.wait(5))
                self.assertEqual(self.ax.injected_prompts, [])
            finally:
                release.set()
            listing.result(timeout=5)
            task.result(timeout=5)
        self.assertEqual(self._result(rid)["status"], "ok")
        self.assertEqual(len(self.ax.injected_prompts), 1)

    def test_control_files_are_private_and_unsafe_inbox_entries_are_rejected(self):
        for path in (self.control, *self.control.iterdir()):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        rid = self._submit()
        req = self.control / "inbox" / f"{rid}.json"
        self.assertEqual(stat.S_IMODE(req.stat().st_mode), 0o600)
        original = req.read_bytes()
        target = self.root.parent / "untouched.json"
        target.write_bytes(original)
        target.chmod(0o600)
        real_fstat = os.fstat

        def foreign_fstat(fd):
            info = real_fstat(fd)
            if info.st_ino == request_inode:
                values = list(info)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return info

        for method, error in (("symlink", "symlink_path_rejected"),
                              ("hardlink", "non_regular_or_hardlinked_file"),
                              ("large", "file_too_large"), ("uid", "control_plane_unavailable")):
            with self.subTest(method=method):
                req.unlink(missing_ok=True)
                if method == "symlink":
                    req.symlink_to(target)
                elif method == "hardlink":
                    os.link(target, req)
                else:
                    req.write_bytes(original if method == "uid" else b"x" * (cp.MAX_REQUEST_BYTES + 1))
                    req.chmod(0o600)
                request_inode = req.lstat().st_ino
                effect = foreign_fstat if method == "uid" else real_fstat
                with mock.patch.object(os, "fstat", side_effect=effect):
                    with self.assertRaisesRegex(tb.BridgeError, f"^{error}$"):
                        self._result(rid)
                    self.daemon.scan()
                self.assertFalse(os.path.lexists(req))
                self.assertEqual(self.ax.injected_prompts, [])
                self.assertEqual(target.read_bytes(), original)

    def test_completed_results_pruned_by_count(self):
        ids = [cp.submit(self.client, self.cfg, "list_chats", {}, via="cli")["requestId"] for _ in range(3)]
        self.daemon.scan()
        now = time.time()
        for i, rid in enumerate(ids):
            os.utime(self.control / "done" / f"{rid}.json", (now + i, now + i))
        with mock.patch.object(cp, "MAX_RESULTS", 2):
            self.daemon.scan()
        self.assertFalse((self.control / "done" / f"{ids[0]}.json").exists())
        self.assertEqual({p.stem for p in (self.control / "done").glob("*.json")}, set(ids[1:]))

    def test_retention_preserves_idempotency_tombstone(self):
        rid = self._submit()
        self.daemon.scan()
        done = self.control / "done" / f"{rid}.json"
        ancient = time.time() - cp.RETENTION_SECONDS - 10
        os.utime(done, (ancient, ancient))
        self.daemon.scan()
        self.assertFalse(done.exists())
        self.assertEqual(self._submit(), rid)
        status = cp.submit(self.client, self.cfg, "create_pair", {}, via="mcp", scope="codex", idempotency_key="key")
        self.assertEqual((status["status"], status["terminalStatus"]), ("expired", "ok"))
        self.daemon.scan()
        self.assertEqual(len(self.ax.injected_prompts), 1)


if __name__ == "__main__":
    unittest.main()
