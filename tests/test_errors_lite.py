"""Focused offline tests for R2 errors-lite deliverables."""
from __future__ import annotations

import datetime
import io
import json
import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from test_bridge import MockAXDriver, OFFLINE_ENV, SCRIPTS_DIR, tb
from test_mcp import McpClient

import error_catalog as ec  # noqa: E402


EXPECTED_CATALOG_CODES = (
    "ax_not_trusted",
    "gemini_not_running",
    "screen_locked",
    "workspace_not_bookmarked_in_gemini",
    "duplicate_gemini_title_in_pairs",
    "pair_has_active_task",
    "unknown_pair_id",
    "daemon_upgrade_required_for_v21_features",
    "control_plane_unavailable",
    "control_request_unconfirmed",
    "pair_limit_reached",
    "invalid_mcp_scope",
    "conflicting_launchagent_for_root",
    "invalid_idempotency_key",
    "gemini_init_failed",
)


class ErrorsLiteTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="t3_gem_errlite_")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.root = self.base / "bridge_root"
        self.la_dir = self.base / "Library/LaunchAgents"
        self.la_dir.mkdir(parents=True)
        self.ax = MockAXDriver()
        self.bridge = tb.Bridge(root=self.root, ax_driver=self.ax, t3_home=str(self.base / "t3"))
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                **OFFLINE_ENV,
                "HOME": str(self.base),
                "GEMINI_TASK_BRIDGE_LAUNCHAGENT_DIR": str(self.la_dir),
            },
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.bridge.setup()
        cfg = self.bridge.config()
        cfg["notifications"] = False
        self.bridge.save_config(cfg)

    def test_01_error_catalog_codes_languages_prefix_and_fallback(self) -> None:
        for code in EXPECTED_CATALOG_CODES:
            for lang in ("zh", "en"):
                desc = ec.describe(code, lang=lang)
                self.assertTrue(desc["message"].strip(), msg=f"empty message for {code} ({lang})")
                self.assertTrue(desc["fix"].strip(), msg=f"empty fix for {code} ({lang})")

        with mock.patch.dict(os.environ, {"GEMINI_TASK_BRIDGE_LANG": "en", "LANG": "zh_CN.UTF-8"}):
            self.assertEqual(ec.detect_lang(), "en")
        with mock.patch.dict(os.environ, {"GEMINI_TASK_BRIDGE_LANG": "", "LANG": "zh_CN.UTF-8"}):
            self.assertEqual(ec.detect_lang(), "zh")
        with mock.patch.dict(os.environ, {"GEMINI_TASK_BRIDGE_LANG": "", "LANG": "en_US.UTF-8"}):
            self.assertEqual(ec.detect_lang(), "en")

        prefixed = ec.describe("gemini_init_failed:title_already_exists", lang="en")
        self.assertIn("title_already_exists", prefixed["message"])
        self.assertIn("gemini-task-bridge doctor", prefixed["fix"])

        fallback_zh = ec.describe("some_unlisted_code", lang="zh")
        self.assertIn("some_unlisted_code", fallback_zh["message"])
        self.assertIn("gemini-task-bridge doctor", fallback_zh["fix"])

    def test_02_cli_and_mcp_errors_include_message_and_fix(self) -> None:
        with mock.patch.dict(os.environ, {"GEMINI_TASK_BRIDGE_LANG": "zh"}), \
                mock.patch("sys.stderr", new=io.StringIO()) as err_buf:
            rc = tb.main(["--root", str(self.root), "pair", "remove", "missing_pair"])
        self.assertEqual(rc, 2)
        cli_err = json.loads(err_buf.getvalue().strip())
        self.assertEqual(cli_err["ok"], False)
        self.assertEqual(cli_err["error"], "unknown_pair_id")
        self.assertTrue(cli_err["message"])
        self.assertIn("gemini-task-bridge pair list --json", cli_err["fix"])

        client = McpClient(
            self.root,
            ["--scope", "codex"],
            {"GEMINI_TASK_BRIDGE_LANG": "en", "GEMINI_TASK_BRIDGE_LAUNCHAGENT_DIR": str(self.la_dir)},
        )
        try:
            client.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
            client.recv()
            client.send({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "gemini_read", "arguments": {"task_id": "missing-task-1"}},
            })
            res = client.recv(timeout=2.0)["result"]
            self.assertTrue(res["isError"])
            payload = json.loads(res["content"][0]["text"])
            self.assertEqual(payload["ok"], False)
            self.assertEqual(payload["error"], "task_not_accessible_in_scope")
            self.assertTrue(payload["message"])
            self.assertIn("gemini-task-bridge doctor", payload["fix"])
        finally:
            self.assertEqual(client.close()[0], 0)

    def test_03_doctor_stages_use_daemon_health_only_and_human_output(self) -> None:
        cfg = self.bridge.config()

        def run_doc(*, as_json: bool = True) -> dict | str:
            with mock.patch("sys.stdout", new=io.StringIO()) as out:
                rc = tb.cmd_doctor(self.bridge, as_json=as_json)
            self.assertEqual(rc, 0)
            return json.loads(out.getvalue()) if as_json else out.getvalue()

        # 1. Agent not loaded even with healthy probes -> daemon
        self.bridge.write_health(self.ax.system_probes(), cfg)
        with mock.patch.object(tb, "agent_info", return_value={"loaded": False}):
            rep = run_doc()
        self.assertEqual((rep["ready"], rep["stage"]), (False, "daemon"))
        self.assertIn("bridgeVersion", rep)
        self.assertIn("daemonHealth", rep)
        self.assertIn("t3CliRequired", rep)

        # 2. Stale health while agent running -> daemon
        stale = self.bridge.read_health()
        stale["lastScanAt"] = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)).isoformat()
        tb.atomic_json(self.root / ".daemon_health.json", stale)
        with mock.patch.object(tb, "agent_info", return_value={"loaded": True, "state": "running"}):
            rep = run_doc()
        self.assertEqual((rep["ready"], rep["stage"]), (False, "daemon"))

        # 3. Stage ladder ignores opposite currentProcess values
        cases = [
            ({"axTrusted": False, "screenLocked": False, "geminiRunning": True, "geminiPid": 1},
             True, False, True, "daemon_accessibility"),
            ({"axTrusted": True, "screenLocked": True, "geminiRunning": True, "geminiPid": 1},
             True, False, True, "screen"),
            ({"axTrusted": True, "screenLocked": False, "geminiRunning": False, "geminiPid": None},
             True, False, True, "gemini"),
            ({"axTrusted": True, "screenLocked": False, "geminiRunning": True, "geminiPid": 1},
             False, True, False, "ok"),
        ]
        for daemon_probes, cur_ax, cur_locked, cur_gem, expected_stage in cases:
            with self.subTest(expected_stage=expected_stage):
                self.bridge.write_health(daemon_probes, cfg)
                self.ax.ax_trusted = cur_ax
                self.ax.screen_locked = cur_locked
                self.ax.gemini_running = cur_gem
                with mock.patch.object(tb, "agent_info", return_value={"loaded": True, "state": "running"}):
                    rep = run_doc()
                self.assertEqual(rep["stage"], expected_stage)
                self.assertEqual(rep["ready"], expected_stage == "ok")
                self.assertTrue(rep["next"].strip())

        # 4. Human-readable non-JSON output
        with mock.patch.dict(os.environ, {"GEMINI_TASK_BRIDGE_LANG": "zh"}), \
                mock.patch.object(tb, "agent_info", return_value={"loaded": True, "state": "running"}):
            text = run_doc(as_json=False)
        with self.assertRaises(ValueError):
            json.loads(text)
        lines = [line for line in text.strip().splitlines() if line.strip()]
        self.assertGreaterEqual(len(lines), 5)
        self.assertIn("✅", text)
        self.assertIn("下一步：", text)

    def test_04_install_agent_rejects_conflicting_label_for_same_root(self) -> None:
        conflict_plist = self.la_dir / "com.xsj.t3-gemini-bridge.plist"
        conflict_plist.write_bytes(plistlib.dumps({
            "Label": "com.xsj.t3-gemini-bridge",
            "EnvironmentVariables": {"T3_GEMINI_BRIDGE_ROOT": str(self.root)},
            "ProgramArguments": ["/usr/bin/python3", "-B", str(self.root / "t3_gemini_bridge.py"), "--root", str(self.root), "run"],
        }))
        conflict_plist.chmod(0o600)

        with mock.patch.dict(os.environ, {"GEMINI_TASK_BRIDGE_LABEL": tb.DEFAULT_LABEL}), \
                mock.patch.object(tb.subprocess, "run", side_effect=AssertionError("must not invoke launchctl")):
            with self.assertRaises(tb.BridgeError) as cm:
                tb.install_agent(self.bridge)
        self.assertEqual(str(cm.exception), "conflicting_launchagent_for_root:com.xsj.t3-gemini-bridge")
        self.assertFalse((self.la_dir / f"{tb.DEFAULT_LABEL}.plist").exists())

        # Unrelated root plist must NOT trigger conflict
        conflict_plist.unlink()
        other_root = self.base / "other_root"
        other_root.mkdir()
        other_plist = self.la_dir / "com.other.unrelated.plist"
        other_plist.write_bytes(plistlib.dumps({
            "Label": "com.other.unrelated",
            "EnvironmentVariables": {"GEMINI_TASK_BRIDGE_ROOT": str(other_root)},
        }))
        other_plist.chmod(0o600)

        with mock.patch.dict(os.environ, {"GEMINI_TASK_BRIDGE_LABEL": tb.DEFAULT_LABEL}), \
                mock.patch.object(tb, "agent_info", return_value={"loaded": False}), \
                mock.patch.object(tb.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                mock.patch("sys.stdout", new=io.StringIO()):
            rc = tb.install_agent(self.bridge)
        self.assertEqual(rc, 0)
        self.assertTrue((self.la_dir / f"{tb.DEFAULT_LABEL}.plist").exists())

    def test_05_install_sh_quotes_python_and_root_with_spaces(self) -> None:
        py_dir = self.base / "py bin"
        py_dir.mkdir()
        py_link = py_dir / "python3"
        os.symlink(sys.executable, py_link)

        space_root = self.base / "my root"
        space_bin = self.base / "my bin"
        env = {
            **os.environ,
            **OFFLINE_ENV,
            "HOME": str(self.base),
            "PATH": f"{py_dir}:{os.environ.get('PATH', '')}",
        }
        for key in ("T3_GEMINI_BRIDGE_ROOT", "GEMINI_TASK_BRIDGE_ROOT", "T3_GEMINI_BRIDGE_LABEL", "GEMINI_TASK_BRIDGE_LABEL"):
            env.pop(key, None)

        res = subprocess.run(
            ["bash", str(SCRIPTS_DIR / "install.sh"), "--root", str(space_root), "--bin-dir", str(space_bin), "--label", "io.test.spaces"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, msg=res.stderr or res.stdout)
        wrapper = space_bin / "gemini-task-bridge"
        self.assertTrue(wrapper.is_file() and os.access(wrapper, os.X_OK))
        self.assertTrue((space_root / "error_catalog.py").is_file())
        self.assertIn(
            f"exec {shlex.quote(str(py_link))} -B {shlex.quote(str(space_root / 'gemini_task_bridge.py'))} \"$@\"",
            wrapper.read_text(encoding="utf-8"),
        )

        st = subprocess.run([str(wrapper), "status", "--json"], env=env, capture_output=True, text=True, check=False)
        self.assertEqual(st.returncode, 0, msg=st.stderr or st.stdout)
        self.assertEqual(json.loads(st.stdout)["queueRoot"], str(space_root.resolve()))

    def test_06_mcp_camelcase_aliases_preserve_schema_and_reject_duplicates(self) -> None:
        with self.bridge.pairs_tx() as pdata:
            pdata["pairs"]["loc_a"] = {
                "pairId": "loc_a",
                "pairUid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "adapter": "local",
                "label": "Local A",
                "t3ThreadId": None,
                "defaultReplyThreadId": None,
                "allowedReplyThreadIds": [],
                "geminiTitle": "Gem-Local-A",
                "route": "gemini",
                "replyMode": "auto",
                "enabled": True,
                "ownerScope": "codex",
            }

        client = McpClient(
            self.root,
            ["--scope", "codex"],
            {"GEMINI_TASK_BRIDGE_LAUNCHAGENT_DIR": str(self.la_dir)},
        )
        try:
            client.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
            client.recv()

            client.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            specs = {t["name"]: t["inputSchema"]["properties"] for t in client.recv()["result"]["tools"]}
            self.assertNotIn("pairId", specs["gemini_send"])
            self.assertNotIn("idempotencyKey", specs["gemini_send"])
            self.assertNotIn("taskId", specs["gemini_read"])

            client.send({
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "gemini_send", "arguments": {"pairId": "loc_a", "message": "Hi", "idempotencyKey": "camel-k1"}},
            })
            r_send = client.recv(timeout=2.0)["result"]
            self.assertFalse(r_send["isError"])
            tid = r_send["structuredContent"]["taskId"]

            client.send({
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "gemini_wait", "arguments": {"taskId": tid, "timeout_seconds": 1}},
            })
            r_wait = client.recv(timeout=3.0)["result"]
            self.assertFalse(r_wait["isError"])
            self.assertEqual(r_wait["structuredContent"]["taskId"], tid)

            client.send({
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "gemini_send", "arguments": {"pairId": "loc_a", "pair_id": "loc_a", "message": "Dup"}},
            })
            r_dup = client.recv(timeout=2.0)
            self.assertEqual(r_dup["error"]["code"], -32602)
        finally:
            self.assertEqual(client.close()[0], 0)


if __name__ == "__main__":
    unittest.main()
