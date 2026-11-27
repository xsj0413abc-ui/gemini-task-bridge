#!/usr/bin/python3
"""Offline unit tests for T3 Code <-> Gemini Desktop 1-to-1 bridge."""
from __future__ import annotations

import argparse
import fcntl
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills/gemini-task-bridge/scripts"
OFFLINE_ENV = {"PYTHONPATH": str(REPO_ROOT / "tests/fixtures/offline"), "PYTHONDONTWRITEBYTECODE": "1"}
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import bridge_ax  # noqa: E402
import gemini_task_bridge as tb  # noqa: E402


class MockAXDriver:
    def __init__(self) -> None:
        self.ax_trusted = True
        self.screen_locked = False
        self.gemini_running = True
        self.gemini_pid = 99999
        self.current_title = "New chat"
        self.conversations: dict[str, dict] = {}
        self.injected_prompts: list[tuple[str, str]] = []
        self.force_open_mismatch = False
        self.force_submit_fail = False
        self.confirmation_card: str | None = None
        self.last_workspace_path: str | None = None

    def system_probes(self) -> dict:
        return {
            "axTrusted": self.ax_trusted,
            "screenLocked": self.screen_locked,
            "geminiRunning": self.gemini_running,
            "geminiPid": self.gemini_pid if self.gemini_running else None,
        }

    def add_conv(self, title: str, badge: str | None = None, chunks: list[str] | None = None) -> None:
        self.conversations[title] = {
            "title": title,
            "badge": badge,
            "busy": False,
            "chunks": list(chunks or []),
        }

    def get_window_title(self, pid: int) -> str | None:
        return self.current_title

    def ensure_main_window_silent(self, pid: int, keep_hidden=None, wait_s: float = 2.5) -> tuple[bool, bool]:
        return True, False

    def list_conversations(self, pid: int) -> dict:
        return {
            "hasWindow": True,
            "currentTitle": self.current_title,
            "conversations": [
                {"raw": k, "title": k, "pinned": False, "badge": v["badge"], "source": "sidebar"}
                for k, v in self.conversations.items()
            ],
        }

    def open_conversation(self, pid: int, target_title: str, timeout_s: float = 3.0) -> tuple[bool, str]:
        if target_title not in self.conversations:
            return False, "conversation_not_found"
        if self.force_open_mismatch:
            self.current_title = "Wrong Window"
            return False, "title_verification_failed"
        self.current_title = target_title
        return True, "sidebar"

    def poll_conversation(self, pid: int) -> dict:
        conv = self.conversations.get(self.current_title, {"busy": False, "chunks": []})
        chunks = list(conv.get("chunks") or [])
        return {
            "has_window": True,
            "window_title": self.current_title,
            "busy": bool(conv.get("busy")),
            "confirmation_card": self.confirmation_card,
            "has_copy_button": True,
            "chunks": chunks,
            "text": "\n".join(chunks),
        }

    def write_prompt_and_submit(self, pid: int, prompt_text: str) -> tuple[bool, str]:
        if self.force_submit_fail:
            return False, "send_button_not_found_or_disabled"
        self.injected_prompts.append((self.current_title, prompt_text))
        conv = self.conversations[self.current_title]
        conv["chunks"].append(prompt_text)
        return True, "submitted_stop_visible"

    def create_task_conversation(
        self,
        pid: int,
        target_title: str,
        init_prompt: str,
        workspace_path: str | None = None,
        wait_s: float = 35.0,
    ) -> tuple[bool, str, str]:
        self.add_conv(target_title)
        self.current_title = target_title
        self.last_workspace_path = workspace_path
        self.injected_prompts.append((target_title, init_prompt))
        return True, target_title, "renamed"


def make_fake_t3_cli(bin_dir: Path, log_path: Path, mode: str = "ok") -> Path:
    cli = bin_dir / "fake_t3.py"
    cli.write_text(
        f"""#!/usr/bin/python3
import json, sys, time
from pathlib import Path
mode = {mode!r}
log = Path({str(log_path)!r})
args = sys.argv[1:]
msg = sys.stdin.read()
if mode == "timeout":
    time.sleep(120)
if mode == "nonzero":
    sys.exit(42)
if mode == "badjson":
    print("NOT_JSON")
    sys.exit(0)
rec = {{"args": args, "message": msg}}
with log.open("a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\\n")
thread_id = args[-2]
print(json.dumps({{"threadId": thread_id, "delivery": "queued", "runId": "run-123"}}))
""",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    return cli


class BridgeUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        # Resolve tempdir because macOS /var -> /private/var triggers no_symlink()
        self.tmp = Path(tempfile.mkdtemp(prefix="t3_gem_test_")).resolve()
        self.root = self.tmp / "bridge_root"
        env_patch = mock.patch.dict(os.environ, {"T3_GEMINI_BRIDGE_LAUNCHAGENT_DIR": str(self.tmp / "launchagents")})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.cli_log = self.tmp / "cli_calls.ndjson"
        self.cli = make_fake_t3_cli(self.tmp, self.cli_log, "ok")
        self.ax = MockAXDriver()
        self.bridge = tb.Bridge(
            root=self.root,
            cli_path=str(self.cli),
            t3_home=str(self.tmp / "t3_home"),
            ax_driver=self.ax,
        )
        self.bridge.setup()
        # Disable macOS osascript notifications during unit tests
        cfg = self.bridge.config()
        cfg["notifications"] = False
        cfg["ax_capture_grace_seconds"] = 0.2
        self.bridge.save_config(cfg)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add_pair(self, pair_id: str, t3_id: str, gem_title: str, route: str = "gemini", reply_mode: str = "auto") -> None:
        with self.bridge.pairs_tx() as pdata:
            pdata["pairs"][pair_id] = {
                "pairId": pair_id,
                "label": pair_id,
                "t3ThreadId": t3_id,
                "defaultReplyThreadId": t3_id,
                "allowedReplyThreadIds": [t3_id],
                "geminiTitle": gem_title,
                "workspacePath": None,
                "route": route,
                "replyMode": reply_mode,
                "enabled": True,
                "createdAt": tb.now_iso(),
                "updatedAt": tb.now_iso(),
            }
        self.ax.add_conv(gem_title)

    def _cli_calls(self) -> list[dict]:
        if not self.cli_log.exists():
            return []
        return [json.loads(line) for line in self.cli_log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_01_sidebar_label_parsing_zh_and_en(self) -> None:
        self.assertEqual(
            bridge_ax.parse_sidebar_row_label("AI策略讨论"),
            {"raw": "AI策略讨论", "title": "AI策略讨论", "pinned": False, "badge": None},
        )
        self.assertEqual(
            bridge_ax.parse_sidebar_row_label("“12_审片质检”，进行中"),
            {"raw": "“12_审片质检”，进行中", "title": "12_审片质检", "pinned": False, "badge": "in_progress"},
        )
        self.assertEqual(
            bridge_ax.parse_sidebar_row_label("“12_审片质检”，需要注意，已固定"),
            {"raw": "“12_审片质检”，需要注意，已固定", "title": "12_审片质检", "pinned": True, "badge": "needs_attention"},
        )
        self.assertEqual(
            bridge_ax.parse_sidebar_row_label('"Review Thread", Ready, Pinned'),
            {"raw": '"Review Thread", Ready, Pinned', "title": "Review Thread", "pinned": True, "badge": "ready"},
        )
        self.assertEqual(
            bridge_ax.parse_sidebar_row_label("03_内容讨论，已固定"),
            {"raw": "03_内容讨论，已固定", "title": "03_内容讨论", "pinned": True, "badge": None},
        )

    def test_02_normalize_window_title(self) -> None:
        self.assertEqual(bridge_ax.normalize_window_title("Gemini - 12_审片质检"), "12_审片质检")
        self.assertEqual(bridge_ax.normalize_window_title("Gemini — My Task"), "My Task")
        self.assertEqual(bridge_ax.normalize_window_title("Gemini"), "")
        self.assertEqual(bridge_ax.normalize_window_title("新对话"), "新对话")

    def test_03_one_to_one_uniqueness_enforced(self) -> None:
        self._add_pair("p1", "thread-1", "Gemini-Chat-1")
        with self.assertRaises(tb.BridgeError) as cm1:
            self._add_pair("p2", "thread-1", "Gemini-Chat-2")
        self.assertEqual(str(cm1.exception), "duplicate_t3_thread_in_pairs")

        with self.assertRaises(tb.BridgeError) as cm2:
            self._add_pair("p3", "thread-3", "Gemini-Chat-1")
        self.assertEqual(str(cm2.exception), "duplicate_gemini_title_in_pairs")

        # Disabled pair should NOT collide
        with self.bridge.pairs_tx() as pdata:
            pdata["pairs"]["p_disabled"] = {
                "pairId": "p_disabled",
                "label": "disabled",
                "t3ThreadId": "thread-1",
                "defaultReplyThreadId": "thread-1",
                "geminiTitle": "Gemini-Chat-1",
                "route": "gemini",
                "replyMode": "auto",
                "enabled": False,
            }

    def test_04_mode_switch_t3_all_and_single_pair_route(self) -> None:
        self._add_pair("role_a", "thread-a", "Gem-A")
        self._add_pair("role_b", "thread-b", "Gem-B")

        # Switch global mode to t3-all
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_mode(self.bridge, "t3-all")
        args = argparse.Namespace(pair="role_a", message="direct T3 test", file=None, stdin=False, task_id="t3all-1", reply_to=None, wait=False, timeout=10, json=True)
        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            rc = tb.cmd_send(self.bridge, args)
        self.assertEqual(rc, 0)
        res = json.loads(out.getvalue())
        self.assertEqual(res["routed"], "t3")
        self.assertEqual(len(self._cli_calls()), 1)
        self.assertEqual(self._cli_calls()[0]["message"], "direct T3 test")
        self.assertEqual(list((self.root / "inbox").glob("*.task.json")), [])

        # Restore hybrid, switch only role_a to t3
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_mode(self.bridge, "hybrid")
        with self.bridge.pairs_tx() as pdata:
            pdata["pairs"]["role_a"]["route"] = "t3"

        args_a = argparse.Namespace(pair="role_a", message="single T3", file=None, stdin=False, task_id="single-t3-1", reply_to=None, wait=False, timeout=10, json=True)
        args_b = argparse.Namespace(pair="role_b", message="single Gemini", file=None, stdin=False, task_id="single-gem-1", reply_to=None, wait=False, timeout=10, json=True)
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_send(self.bridge, args_a)
            tb.cmd_send(self.bridge, args_b)
        self.assertEqual(len(self._cli_calls()), 2)
        self.assertTrue((self.root / "inbox/single-gem-1.task.json").exists())

    def test_05_split_sentinel_prompt_echo_immunity_and_extraction(self) -> None:
        task_id = "task-echo-001"
        framed = tb.build_framed_prompt(task_id, "p1", "thread-1", "Check segment", self.root / "outbox" / f"{task_id}.json")
        # Prompt MUST NOT contain unbroken done sentinel
        self.assertNotIn(f"[[T3_BRIDGE_DONE:{task_id}]]", framed)
        self.assertIn(f"[[T3_BRIDGE_START:{task_id}]]", framed)

        chunks = [
            "Old turn content A",
            framed,
            "Gemini said",
            "Show thinking",
            "First line of real findings",
            "Conclusion: no blockers.",
            f"[[T3_BRIDGE_DONE:{task_id}]]",
            "Copy",
        ]
        has_sent, clean = tb.extract_gemini_reply("", task_id, baseline_chunks=["Old turn content A"], current_chunks=chunks)
        self.assertTrue(has_sent)
        self.assertEqual(clean, "First line of real findings\nConclusion: no blockers.")

        # Only echoed prompt, no reply yet -> empty
        has_sent2, clean2 = tb.extract_gemini_reply("", task_id, baseline_chunks=["Old turn content A"], current_chunks=["Old turn content A", framed])
        self.assertFalse(has_sent2)
        self.assertEqual(clean2, "")

    def test_06_title_mismatch_fail_closed(self) -> None:
        self._add_pair("p1", "thread-1", "Gem-Target")
        self.ax.force_open_mismatch = True
        args = argparse.Namespace(pair="p1", message="no crosstalk", file=None, stdin=False, task_id="mismatch-1", reply_to=None, wait=False, timeout=10, json=True)
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_send(self.bridge, args)
        self.bridge.scan(recover=True)
        # Task must remain in inbox and NOT injected into wrong Gemini window
        self.assertEqual(self.ax.injected_prompts, [])
        self.assertTrue((self.root / "inbox/mismatch-1.task.json").exists())
        self.assertFalse((self.root / "inbox/in_flight/mismatch-1.task.json").exists())

    def test_07_full_auto_roundtrip_via_ax_capture_and_dedup(self) -> None:
        self._add_pair("p1", "thread-1", "Gem-Auto")
        args = argparse.Namespace(pair="p1", message="Run QA", file=None, stdin=False, task_id="auto-rt-1", reply_to=None, wait=False, timeout=10, json=True)
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_send(self.bridge, args)

        # Scan 1: dispatches from inbox -> inbox/in_flight
        self.bridge.scan(recover=True)
        self.assertEqual(len(self.ax.injected_prompts), 1)
        self.assertTrue((self.root / "inbox/in_flight/auto-rt-1.task.json").exists())

        # Backdate dispatchedMono & simulate Gemini reply completion
        with self.bridge.ledger_tx() as ld:
            ld["dispatches"]["auto-rt-1"]["dispatchedMono"] = time.monotonic() - 10.0
        self.ax.conversations["Gem-Auto"]["chunks"].extend([
            "QA result: visuals continuous and audio clear.",
            "[[T3_BRIDGE_DONE:auto-rt-1]]",
        ])

        # Scan 2: sets idleSinceMono
        self.bridge.scan(recover=False)
        time.sleep(0.25)
        # Scan 3: grace passed, polls & sets stableCount=1
        self.bridge.scan(recover=False)
        time.sleep(0.55)
        # Scan 4: confirms stability -> writes outbox & delivers to T3!
        self.bridge.scan(recover=False)

        calls = self._cli_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("[Bridge Reply: p1 -> T3] Task: auto-rt-1", calls[0]["message"])
        self.assertIn("QA result: visuals continuous and audio clear.", calls[0]["message"])
        self.assertTrue((self.root / "sent/auto-rt-1.receipt.json").exists())
        self.assertTrue((self.root / "inbox/done/auto-rt-1.task.json").exists())

        # Duplicate outbox drop must go to dedup without second CLI call
        tb.atomic_json(self.root / "outbox/auto-rt-1.json", {
            "taskId": "auto-rt-1",
            "pairId": "p1",
            "message": "Duplicate receipt should be suppressed",
        })
        self.bridge.scan(recover=False)
        self.assertEqual(len(self._cli_calls()), 1)
        self.assertTrue((self.root / "dedup/auto-rt-1.json").exists())

    def test_08_per_pair_fifo_and_cross_pair_concurrency(self) -> None:
        self._add_pair("p1", "thread-1", "Gem-1")
        self._add_pair("p2", "thread-2", "Gem-2")
        for tid, pid in (("t-01", "p1"), ("t-02", "p1"), ("t-03", "p2")):
            with mock.patch("sys.stdout", new=io.StringIO()):
                tb.cmd_send(self.bridge, argparse.Namespace(pair=pid, message=f"msg {tid}", file=None, stdin=False, task_id=tid, reply_to=None, wait=False, timeout=10, json=True))
        self.bridge.scan(recover=True)
        # t-01 (p1) and t-03 (p2) should dispatch; t-02 (p1) waits in inbox!
        self.assertTrue((self.root / "inbox/in_flight/t-01.task.json").exists())
        self.assertTrue((self.root / "inbox/in_flight/t-03.task.json").exists())
        self.assertTrue((self.root / "inbox/t-02.task.json").exists())

    def test_09_needs_attention_pauses_capture(self) -> None:
        self._add_pair("p1", "thread-1", "Gem-Attn")
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_send(self.bridge, argparse.Namespace(pair="p1", message="Needs approval", file=None, stdin=False, task_id="attn-1", reply_to=None, wait=False, timeout=10, json=True))
        self.bridge.scan(recover=True)
        with self.bridge.ledger_tx() as ld:
            ld["dispatches"]["attn-1"]["dispatchedMono"] = time.monotonic() - 10.0
        self.ax.conversations["Gem-Attn"]["badge"] = "needs_attention"
        self.bridge.scan(recover=False)
        with tb.file_lock(self.root / ".ledger.lock"):
            ld = self.bridge.ledger()
        self.assertEqual(ld["dispatches"]["attn-1"]["status"], "needs_attention")
        self.assertEqual(len(self._cli_calls()), 0)

    def test_10_outbox_v1_compat_and_recovery_unknown_timeout(self) -> None:
        self._add_pair("director", "dir-thread-1", "Gem-Dir")
        # Test interrupted 'sending' recovery -> unknown_timeout, no retry
        stale = self.root / "sending/stale-1.json"
        tb.atomic_json(stale, {"taskId": "stale-1", "pairId": "director", "message": "interrupted"})
        with self.bridge.ledger_tx() as ld:
            ld["tasks"]["stale-1"] = {"status": "sending", "updatedAt": tb.now_iso()}
        self.bridge.scan(recover=True)
        with tb.file_lock(self.root / ".ledger.lock"):
            ld = self.bridge.ledger()
        self.assertEqual(ld["tasks"]["stale-1"]["status"], "unknown_timeout")
        self.assertTrue((self.root / "failed/stale-1.json").exists())
        self.assertEqual(len(self._cli_calls()), 0)

        # Test v1 receipt format
        tb.atomic_json(self.root / "outbox/v1-msg-1.json", {
            "taskId": "v1-msg-1",
            "role": "reviewer",
            "target": "director",
            "message": "v1 compatible receipt",
        })
        self.bridge.scan(recover=False)
        self.assertEqual(len(self._cli_calls()), 1)
        self.assertEqual(self._cli_calls()[0]["message"], "v1 compatible receipt")

    def test_11_cli_nonzero_marks_failed(self) -> None:
        self._add_pair("p1", "thread-1", "Gem-1")
        bad_cli = make_fake_t3_cli(self.tmp, self.cli_log, "nonzero")
        b2 = tb.Bridge(root=self.root, cli_path=str(bad_cli), t3_home=str(self.tmp / "t3_home"), ax_driver=self.ax)
        b2.setup()
        tb.atomic_json(self.root / "outbox/fail-1.json", {"taskId": "fail-1", "pairId": "p1", "message": "failure test"})
        b2.scan(recover=False)
        with tb.file_lock(self.root / ".ledger.lock"):
            ld = b2.ledger()
        self.assertEqual(ld["tasks"]["fail-1"]["status"], "failed")
        self.assertTrue((self.root / "failed/fail-1.json").exists())

    def test_12_security_symlink_oversized_and_unauthorized_reply(self) -> None:
        self._add_pair("p1", "thread-1", "Gem-1")
        # Symlink in outbox must be ignored (not followed or moved)
        target = self.tmp / "outside.json"
        target.write_text('{"taskId":"sym-1","pairId":"p1","message":"x"}', encoding="utf-8")
        sym = self.root / "outbox/sym-1.json"
        sym.symlink_to(target)
        self.bridge.scan(recover=False)
        self.assertTrue(sym.is_symlink())
        self.assertEqual(len(self._cli_calls()), 0)

        # Oversized file in outbox -> failed
        big = self.root / "outbox/big-1.json"
        big.write_text("x" * 70000, encoding="utf-8")
        self.bridge.scan(recover=False)
        self.assertTrue((self.root / "failed/big-1.json").exists())

        # Unauthorized reply_to rejected
        with self.assertRaises(tb.BridgeError) as cm:
            tb.cmd_send(self.bridge, argparse.Namespace(pair="p1", message="hi", file=None, stdin=False, task_id="auth-1", reply_to="evil-thread-999", wait=False, timeout=10, json=True))
        self.assertEqual(str(cm.exception), "reply_target_not_whitelisted")

    def test_13_process_lock_prevents_second_runner(self) -> None:
        with tb.file_lock(self.root / ".process.lock", nonblocking=True):
            with mock.patch("sys.stdout", new=io.StringIO()):
                rc = self.bridge.execute(continuous=False)
            self.assertEqual(rc, 3)

    def test_14_ax_not_trusted_blocks_dispatch_without_dropping_inbox(self) -> None:
        self._add_pair("p1", "thread-1", "Gem-1")
        self.ax.ax_trusted = False
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_send(self.bridge, argparse.Namespace(pair="p1", message="waiting for ax", file=None, stdin=False, task_id="wait-ax-1", reply_to=None, wait=False, timeout=10, json=True))
        self.bridge.scan(recover=True)
        self.assertTrue((self.root / "inbox/wait-ax-1.task.json").exists())
        health = self.bridge.read_health()
        self.assertFalse(health["axTrusted"])

    def test_15_reply_truncation_stays_within_limits(self) -> None:
        long_reply = "\n".join(f"Line {i}: " + ("data" * 10) for i in range(600))
        has_sent, clean = tb.extract_gemini_reply(
            "",
            "trunc-1",
            current_chunks=["[[T3_BRIDGE_START:trunc-1]]", long_reply, "[[T3_BRIDGE_DONE:trunc-1]]"],
            max_bytes=4096,
            max_lines=100,
        )
        self.assertTrue(has_sent)
        self.assertLessEqual(len(clean.encode("utf-8")), 4096)
        self.assertLessEqual(len(clean.splitlines()), 100)
        self.assertIn("truncated", clean)

    def test_16_pick_main_window_skips_search_popup_and_dialog(self) -> None:
        idx = bridge_ax.pick_main_window_index([
            {"role": "AXWindow", "subrole": "AXStandardWindow", "title": "", "is_minichat": False},
            {"role": "AXUnknown", "subrole": "", "title": "", "is_minichat": False},
            {"role": "AXWindow", "subrole": "AXStandardWindow", "title": "Gemini - Task Alpha", "is_minichat": False},
            {"role": "AXWindow", "subrole": "AXDialog", "title": "Usage limit", "is_minichat": False},
        ])
        self.assertEqual(idx, 2)

        idx_fallback = bridge_ax.pick_main_window_index([
            {"role": "AXWindow", "subrole": "AXStandardWindow", "title": "", "is_minichat": False},
            {"role": "AXWindow", "subrole": "AXDialog", "title": "Usage limit", "is_minichat": False},
        ])
        self.assertEqual(idx_fallback, 0)

    def test_17_chrome_text_filters_zh_and_en_followup_placeholders(self) -> None:
        task_id = "chrome-bilingual-1"
        has_sent, clean = tb.extract_gemini_reply(
            "",
            task_id,
            current_chunks=[
                f"[[T3_BRIDGE_START:{task_id}]]\n--- TASK ---\nhello",
                "Gemini 说",
                "Actual output body",
                "接下来做什么？",
                "What's next?",
                f"[[T3_BRIDGE_DONE:{task_id}]]",
            ],
        )
        self.assertTrue(has_sent)
        self.assertEqual(clean, "Actual output body")

    def test_18_pair_init_forwards_workspace_path(self) -> None:
        ws = self.tmp / "project_ws"
        ws.mkdir()
        args = argparse.Namespace(
            pair_cmd="init",
            id="ws_pair",
            t3_thread="thread-ws-1",
            reply_to=None,
            label="Workspace Test",
            gemini_title="Gemini-WS-Task",
            workspace=str(ws),
            init_prompt=None,
            reply_mode="auto",
            wait_sec=10.0,
        )
        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            rc = tb.cmd_pair(self.bridge, args)
        self.assertEqual(rc, 0)
        res = json.loads(out.getvalue())
        self.assertEqual(res["pair"]["workspacePath"], str(ws.resolve()))
        self.assertEqual(self.ax.last_workspace_path, str(ws.resolve()))

    def test_19_extract_reply_ignores_folded_prior_turns_without_start_marker(self) -> None:
        baseline = [
            "Turn 2: [[T3_BRIDGE_START:t-1]] ...",
            "READY",
            "Old turn reply\n[[T3_BRIDGE_DONE:t-1]]",
        ]
        current = [
            "READY",
            "Old turn reply\n[[T3_BRIDGE_DONE:t-1]]",
            "Only new turn reply\n[[T3_BRIDGE_DONE:t-2]]",
        ]
        has_sent, clean = tb.extract_gemini_reply(
            "",
            "t-2",
            baseline_chunks=baseline,
            current_chunks=current,
        )
        self.assertTrue(has_sent)
        self.assertEqual(clean, "Only new turn reply")

    def test_20_extract_reply_ignores_non_chronological_nav_buttons_and_prior_turns(self) -> None:
        baseline = [
            "Turn 2: [[T3_BRIDGE_START:t-1]] ...",
            "Turn 3: [[T3_BRIDGE_START:t-2]] ...",
            "READY",
            "ALPHA\n[[T3_BRIDGE_DONE:t-1]]",
            "001\n[[T3_BRIDGE_DONE:t-2]]",
        ]
        current = [
            "Turn 2: [[T3_BRIDGE_START:t-1]] ...",
            "Turn 3: [[T3_BRIDGE_START:t-2]] ...",
            "Turn 4: [[T3_BRIDGE_START:t-4]] ...",
            "READY",
            "ALPHA\n[[T3_BRIDGE_DONE:t-1]]",
            "001\n[[T3_BRIDGE_DONE:t-2]]",
            "CLEAR\n[[T3_BRIDGE_DONE:t-4]]",
        ]
        has_sent, clean = tb.extract_gemini_reply(
            "",
            "t-4",
            baseline_chunks=baseline,
            current_chunks=current,
        )
        self.assertTrue(has_sent)
        self.assertEqual(clean, "CLEAR")

        has_sent2, clean2 = tb.extract_gemini_reply(
            "",
            "t-1",
            baseline_chunks=["READY"],
            current_chunks=[
                "Turn 2: [[T3_BRIDGE_START:t-1]] ...",
                "READY",
                "ALPHA\n[[T3_BRIDGE_DONE:t-1]]",
            ],
        )
        self.assertTrue(has_sent2)
        self.assertEqual(clean2, "ALPHA")

    def test_21_extract_reply_preserves_identical_reply_in_chronological_transcript(self) -> None:
        baseline = ["Initial instruction", "OK"]
        current = [
            "Initial instruction",
            "OK",
            "[[T3_BRIDGE_START:t-2]]\n--- TASK ---\nConfirm",
            "OK",
        ]
        has_sent, clean = tb.extract_gemini_reply(
            "",
            "t-2",
            baseline_chunks=baseline,
            current_chunks=current,
        )
        self.assertFalse(has_sent)
        self.assertEqual(clean, "OK")

    def test_22_resolve_launchagent_python_avoids_hardlink_shim(self) -> None:
        py = tb.resolve_launchagent_python()
        self.assertTrue(Path(py).is_file())
        self.assertTrue(os.access(py, os.X_OK))
        app_py = Path("/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python")
        if sys.platform == "darwin" and app_py.is_file():
            self.assertNotEqual(py, "/usr/bin/python3")
            self.assertEqual(os.stat(py).st_nlink, 1)

    def test_23_resolve_default_t3_cli_env_and_path_fallback(self) -> None:
        fake_home = self.tmp / "fake_t3_home"
        (fake_home / "bin").mkdir(parents=True)
        local_cli = fake_home / "bin/t3"
        local_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        local_cli.chmod(0o755)

        with mock.patch.dict(os.environ, {"T3_GEMINI_BRIDGE_CLI": "/custom/env/t3"}, clear=False):
            self.assertEqual(tb.resolve_default_t3_cli(str(fake_home)), "/custom/env/t3")

        with mock.patch.dict(os.environ, {"T3_GEMINI_BRIDGE_CLI": ""}, clear=False):
            self.assertEqual(tb.resolve_default_t3_cli(str(fake_home)), str(local_cli))

        empty_home = self.tmp / "empty_t3_home"
        path_bin = self.tmp / "path_bin"
        path_bin.mkdir()
        path_cli = path_bin / "t3"
        path_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path_cli.chmod(0o755)
        with mock.patch.dict(os.environ, {"T3_GEMINI_BRIDGE_CLI": "", "PATH": str(path_bin)}, clear=False):
            self.assertEqual(tb.resolve_default_t3_cli(str(empty_home)), str(path_cli))

    def test_24_child_env_uses_dynamic_user_and_no_hardcoded_personal_paths(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": "/tmp/example-home", "USER": "alice", "LOGNAME": "alice"}, clear=False):
            env = tb.child_env()
            self.assertEqual(env["HOME"], "/tmp/example-home")
            self.assertEqual(env["USER"], "alice")
            self.assertEqual(env["LOGNAME"], "alice")

    def test_25_install_script_dry_isolated_root_and_preserves_state(self) -> None:
        install_sh = SCRIPTS_DIR / "install.sh"
        iso_root = self.tmp / "iso share's root"
        iso_bin = self.tmp / "iso_bin"
        offline_env = {**os.environ, **OFFLINE_ENV, "HOME": str(self.tmp), "T3_GEMINI_BRIDGE_ROOT": str(iso_root)}

        res1 = subprocess.run(
            ["bash", str(install_sh), "--root", str(iso_root), "--bin-dir", str(iso_bin), "--label", "io.test.bridge"],
            env=offline_env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res1.returncode, 0, msg=res1.stderr or res1.stdout)
        wrapper = iso_bin / "t3-gemini-bridge"
        self.assertTrue(wrapper.is_file() and os.access(wrapper, os.X_OK))
        self.assertEqual(wrapper.read_bytes(), (iso_bin / "gemini-task-bridge").read_bytes())
        self.assertEqual((iso_root / "t3_gemini_bridge.py").read_bytes(), (iso_root / "gemini_task_bridge.py").read_bytes())

        status_res = subprocess.run([str(wrapper), "status", "--json"], env=offline_env, capture_output=True, text=True, check=False)
        self.assertEqual(status_res.returncode, 0, msg=status_res.stderr)
        status_json = json.loads(status_res.stdout)
        self.assertEqual(status_json["queueRoot"], str(iso_root))
        self.assertEqual(status_json["globalMode"], "hybrid")

        # Seed custom pair in pairs.json and verify re-running install.sh does NOT overwrite it
        pairs_file = iso_root / "pairs.json"
        seeded = {
            "schemaVersion": 2,
            "pairs": {
                "keep_me": {
                    "pairId": "keep_me",
                    "label": "Keep",
                    "t3ThreadId": "thread-keep",
                    "defaultReplyThreadId": "thread-keep",
                    "geminiTitle": "Keep Chat",
                    "route": "gemini",
                    "replyMode": "auto",
                    "enabled": True,
                }
            },
        }
        pairs_file.write_text(json.dumps(seeded), encoding="utf-8")

        res2 = subprocess.run(
            ["bash", str(install_sh), "--root", str(iso_root), "--bin-dir", str(iso_bin), "--label", "io.test.bridge"],
            env=offline_env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res2.returncode, 0, msg=res2.stderr or res2.stdout)
        after = json.loads(pairs_file.read_text(encoding="utf-8"))
        self.assertIn("keep_me", after["pairs"])

    def test_install_add_to_path_backs_up_zshrc_and_does_not_duplicate(self) -> None:
        rc = self.tmp / ".zshrc"
        original = b"# Existing settings"
        rc.write_bytes(original)
        backup = self.tmp / ".zshrc.gemini-task-bridge.bak"
        env = {
            **os.environ,
            **OFFLINE_ENV,
            "HOME": str(self.tmp),
            "PATH": "/usr/bin:/bin",
            "GEMINI_TASK_BRIDGE_ROOT": str(self.root),
        }
        cmd = [
            "bash", str(SCRIPTS_DIR / "install.sh"), "--label", "io.test.bridge",
            "--add-to-path", "--no-open",
        ]
        expected = original + b'\nexport PATH="$HOME/.local/bin:$PATH"\n'
        for _ in range(2):
            result = subprocess.run(cmd, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertEqual(rc.read_bytes(), expected)
            self.assertEqual(backup.read_bytes(), original)

    def test_26_invalid_launchagent_label_rejected(self) -> None:
        for bad_label in ("../evil", "bad/label", "", ".hidden", "has space"):
            with self.assertRaises(tb.BridgeError) as cm:
                tb._validated_label(bad_label)
            self.assertEqual(str(cm.exception), "invalid_launchagent_label")

        lbl, plist_path = tb._validated_label("io.example.t3-gemini-bridge")
        self.assertEqual(lbl, "io.example.t3-gemini-bridge")
        self.assertEqual(plist_path.name, "io.example.t3-gemini-bridge.plist")

    def test_default_root_prefers_existing_new_directory_without_moving_legacy_data(self) -> None:
        share = self.tmp / ".local/share"
        modern, legacy = share / "gemini-task-bridge", share / "t3-gemini-bridge"
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp)}, clear=True):
            self.assertEqual(tb.resolve_default_root(), modern)
            legacy.mkdir(parents=True)
            self.assertEqual(tb.resolve_default_root(), legacy)
            modern.mkdir()
            self.assertEqual(tb.resolve_default_root(), modern)
            with mock.patch.dict(os.environ, {"GEMINI_TASK_BRIDGE_ROOT": str(self.root)}):
                self.assertEqual(tb.resolve_default_root(), self.root)
        self.assertTrue(legacy.is_dir())

    def test_default_label_reuses_matching_legacy_and_new_plists(self) -> None:
        directory = self.tmp / "test-launchagents"
        directory.mkdir()
        legacy_label = "io.test.legacy"
        modern_label = "io.test.modern"
        other_root = self.tmp / "other_root"
        for label, prefix, root in (
            (legacy_label, "T3_GEMINI_BRIDGE", self.root),
            (modern_label, "GEMINI_TASK_BRIDGE", other_root),
        ):
            (directory / f"{label}.plist").write_bytes(plistlib.dumps({
                "Label": label, "EnvironmentVariables": {prefix + "_ROOT": str(root)},
            }))
        env = {"HOME": str(self.tmp), "GEMINI_TASK_BRIDGE_LAUNCHAGENT_DIR": str(directory)}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(tb.resolve_default_label(self.root), legacy_label)
            self.assertEqual(tb.resolve_default_label(other_root), modern_label)
            self.assertEqual(tb.resolve_default_label(self.tmp / "unregistered"), tb.DEFAULT_LABEL)
            with mock.patch.dict(os.environ, {"GEMINI_TASK_BRIDGE_LABEL": "io.test.override"}):
                self.assertEqual(tb.resolve_default_label(self.root), "io.test.override")

    def test_bridge_env_falls_back_only_when_new_prefix_is_unset(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(tb.bridge_env("MCP_ALLOW_PAIRS"))
            os.environ["T3_GEMINI_BRIDGE_MCP_ALLOW_PAIRS"] = "legacy"
            self.assertEqual(tb.bridge_env("MCP_ALLOW_PAIRS"), "legacy")
            os.environ["GEMINI_TASK_BRIDGE_MCP_ALLOW_PAIRS"] = "modern"
            self.assertEqual(tb.bridge_env("MCP_ALLOW_PAIRS"), "modern")
            os.environ["GEMINI_TASK_BRIDGE_MCP_ALLOW_PAIRS"] = ""
            self.assertEqual(tb.bridge_env("MCP_ALLOW_PAIRS"), "")


class BridgePhase1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="t3_gem_p1_")).resolve()
        self.root = self.tmp / "bridge_root"
        self.la_dir = self.tmp / "launchagents"
        self.la_dir.mkdir(parents=True)
        self._env_patch = mock.patch.dict(os.environ, {"T3_GEMINI_BRIDGE_LAUNCHAGENT_DIR": str(self.la_dir)}, clear=False)
        self._env_patch.start()
        agent_patch = mock.patch.object(tb, "agent_info", return_value={"loaded": False})
        agent_patch.start()
        self.addCleanup(agent_patch.stop)
        self.cli_log = self.tmp / "cli_calls.ndjson"
        self.cli = make_fake_t3_cli(self.tmp, self.cli_log, "ok")
        self.ax = MockAXDriver()
        self.bridge = tb.Bridge(
            root=self.root,
            cli_path=str(self.cli),
            t3_home=str(self.tmp / "t3_home"),
            ax_driver=self.ax,
        )
        self.bridge.setup()
        cfg = self.bridge.config()
        cfg["notifications"] = False
        cfg["ax_capture_grace_seconds"] = 0.2
        self.bridge.save_config(cfg)

    def tearDown(self) -> None:
        self._env_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cli_calls(self) -> list[dict]:
        if not self.cli_log.exists():
            return []
        return [json.loads(line) for line in self.cli_log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_p0_1_upgrade_gate_blocks_old_daemon_and_keepalive_plist(self) -> None:
        # 1. Old v2.0 daemon holding .process.lock + legacy .daemon_health.json (no capabilities)
        legacy_health = {
            "schemaVersion": 1,
            "pid": 42424,
            "startedAt": tb.now_iso(),
            "lastScanAt": tb.now_iso(),
            "axTrusted": True,
            "screenLocked": False,
            "geminiRunning": True,
            "geminiPid": 99999,
            "globalMode": "hybrid",
            "lastError": None,
        }
        tb.atomic_json(self.root / ".daemon_health.json", legacy_health)
        with tb.file_lock(self.root / ".process.lock", nonblocking=True) as fd:
            os.ftruncate(fd, 0)
            os.write(fd, b"42424")
            with self.assertRaises(tb.BridgeError) as cm:
                tb.cmd_pair(
                    self.bridge,
                    argparse.Namespace(
                        pair_cmd="add", id="loc1", adapter="local", t3_thread=None,
                        gemini_title="Gem-Loc-1", reply_to=None, label=None, workspace=None,
                        route="gemini", reply_mode="auto", force=False,
                    ),
                )
            self.assertEqual(str(cm.exception), "daemon_upgrade_required_for_v21_features")

            # Cross-version legacy T3 send works via compat write path with st_nlink == 1 & no overwritten dispatch
            with self.bridge.pairs_tx() as pdata:
                pdata["pairs"]["t3_legacy"] = {
                    "pairId": "t3_legacy", "label": "t3_legacy", "t3ThreadId": "t-1",
                    "defaultReplyThreadId": "t-1", "geminiTitle": "Gem-T3", "route": "gemini",
                    "replyMode": "auto", "enabled": True,
                }
            res = self.bridge.submit_task(pair_id="t3_legacy", message="legacy compat send")
            inbox_f = Path(res["inboxFile"])
            self.assertTrue(inbox_f.exists())
            self.assertEqual(os.stat(inbox_f).st_nlink, 1)
            with tb.file_lock(self.root / ".ledger.lock"):
                self.assertNotIn(res["taskId"], self.bridge.ledger()["dispatches"])

        # 2. Unlocked .process.lock BUT KeepAlive v2.0 LaunchAgent loaded=True -> STILL blocked!
        old_script = self.tmp / "old_v20_bridge.py"
        old_script.write_text("#!/usr/bin/python3\n# v2.0 script without BRIDGE_VERSION\n", encoding="utf-8")
        plist_file = self.la_dir / "io.test.old.plist"
        import plistlib
        plist_file.write_bytes(plistlib.dumps({
            "Label": "io.test.old",
            "ProgramArguments": ["/usr/bin/python3", str(old_script), "--root", str(self.root), "run"],
            "EnvironmentVariables": {"T3_GEMINI_BRIDGE_ROOT": str(self.root)},
            "KeepAlive": True,
        }))
        with mock.patch.object(tb, "agent_info", return_value={"loaded": True, "state": "spawn scheduled"}):
            ok, reason = self.bridge.check_v21_runtime_for_root(self.bridge.config())
            self.assertFalse(ok)
            self.assertEqual(reason, "daemon_upgrade_required_for_v21_features")

        # 3. Controlled offline upgraded plist (loaded=False, v2.1 script on disk) -> allowed
        new_script = self.tmp / "new_v21_bridge.py"
        new_script.write_text('#!/usr/bin/python3\nBRIDGE_VERSION = "2.1.0"\n', encoding="utf-8")
        plist_file.write_bytes(plistlib.dumps({
            "Label": "io.test.old",
            "ProgramArguments": ["/usr/bin/python3", str(new_script), "--root", str(self.root), "run"],
            "EnvironmentVariables": {"T3_GEMINI_BRIDGE_ROOT": str(self.root)},
        }))
        with mock.patch.object(tb, "agent_info", return_value={"loaded": False}):
            ok2, _ = self.bridge.check_v21_runtime_for_root(self.bridge.config())
            self.assertTrue(ok2)

    def test_p0_a_single_link_publish_continuous_sweep_and_live_writer_protection(self) -> None:
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="loc1", adapter="local", t3_thread=None,
                    gemini_title="Gem-Loc-1", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
        self.ax.add_conv("Gem-Loc-1")

        # Simulate Stage 2 crash after ledger reservation but before Stage 3 publish
        tid = "staged-recover-1"
        msg = "Hello single-link world"
        req_sha, _ = tb.sha256_text(msg)
        stage_name = f"._stage.{tid}.deaddead.task.json"
        stage_path = self.root / "inbox" / stage_name
        tb.atomic_json(stage_path, {
            "schemaVersion": 2, "taskId": tid, "pairId": "loc1",
            "replyThreadId": None, "replyMode": "auto", "message": msg, "createdAt": tb.now_iso(),
        })
        with tb.file_lock(self.root / ".pairs.lock"):
            p_uid = self.bridge.pairs()["pairs"]["loc1"]["pairUid"]
        with self.bridge.ledger_tx() as ld:
            ld["dispatches"][tid] = {
                "status": "queued", "deliveryKind": "gemini_reply", "pairId": "loc1",
                "pairUid": p_uid, "adapter": "local", "geminiTitle": "Gem-Loc-1",
                "replyThreadId": None, "requestSha256": req_sha,
                "stagingFile": stage_name, "published": False, "createdAt": tb.now_iso(),
            }

        # Live writer holding flock on an unreferenced staging file MUST NOT be swept
        live_orphan = self.root / "inbox/._stage.livewriter-1.beefbeef.task.json"
        live_fd = os.open(str(live_orphan), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        fcntl.flock(live_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            # Running continuous scan(recover=False) reconciles crashed staged intake without restart!
            self.bridge.scan(recover=False)
            self.assertTrue(live_orphan.exists())
            self.assertFalse(stage_path.exists())
            self.assertTrue((self.root / "inbox/in_flight" / f"{tid}.task.json").exists())
            self.assertEqual(os.stat(self.root / "inbox/in_flight" / f"{tid}.task.json").st_nlink, 1)
        finally:
            os.close(live_fd)

        # Once live writer releases lock, next scan sweeps orphan staging safely
        self.bridge.scan(recover=False)
        self.assertFalse(live_orphan.exists())

        # Duplicate task_id submit_task fails cleanly with EEXIST protection
        with self.assertRaises(tb.BridgeError) as cm:
            self.bridge.submit_task(pair_id="loc1", message="dup", task_id=tid)
        self.assertEqual(str(cm.exception), "duplicate_task_id")

    def test_condition1_step_c_relock_prevents_submitting_to_wrong_chat(self) -> None:
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="loc1", adapter="local", t3_thread=None,
                    gemini_title="Gem-Target", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
        self.ax.add_conv("Gem-Target")
        self.ax.add_conv("Gem-Other")

        # Hook open_conversation so on the 2nd open (Step C re-lock), window title drifts to Gem-Other
        orig_open = self.ax.open_conversation
        open_calls = [0]

        def drifting_open(pid: int, title: str, timeout_s: float = 3.0) -> tuple[bool, str]:
            open_calls[0] += 1
            if open_calls[0] >= 2:
                self.ax.current_title = "Gem-Other"
                return True, "sidebar"
            return orig_open(pid, title, timeout_s)

        self.ax.open_conversation = drifting_open
        res = self.bridge.submit_task(pair_id="loc1", message="Target only", task_id="cond1-1")
        self.assertEqual(res["taskId"], "cond1-1")
        self.bridge.scan(recover=False)
        self.assertEqual(self.ax.injected_prompts, [])
        with tb.file_lock(self.root / ".ledger.lock"):
            d = self.bridge.ledger()["dispatches"]["cond1-1"]
        self.assertEqual(d["status"], "queued")

    def test_condition2_press_uncertain_no_autorepress_and_same_scan_reconcile(self) -> None:
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="loc1", adapter="local", t3_thread=None,
                    gemini_title="Gem-Uncertain", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
        self.ax.add_conv("Gem-Uncertain")
        press_calls = [0]

        def uncertain_submit(pid: int, prompt_text: str) -> tuple[bool, str]:
            press_calls[0] += 1
            return False, "submit_press_uncertain"

        self.ax.write_prompt_and_submit = uncertain_submit
        self.bridge.submit_task(pair_id="loc1", message="Uncertain press", task_id="unc-1")
        self.bridge.scan(recover=False)
        # Pressed ONCE, zero auto-repress, and continuous scan reconciled dispatching -> unknown_timeout
        self.assertEqual(press_calls[0], 1)
        st = self.bridge.get_task_status("unc-1")
        self.assertEqual(st["status"], "unknown_timeout")
        self.assertEqual(st["reason"], "dispatch_submit_outcome_unknown")
        self.assertTrue((self.root / "failed/unc-1.task.json").exists())

        # Case 2: pressed once, Gemini actually received sentinel even though submit returned uncertain
        press_calls[0] = 0

        def uncertain_but_sent(pid: int, prompt_text: str) -> tuple[bool, str]:
            press_calls[0] += 1
            self.ax.conversations["Gem-Uncertain"]["chunks"].append(prompt_text)
            return False, "submit_press_uncertain"

        self.ax.write_prompt_and_submit = uncertain_but_sent
        self.bridge.submit_task(pair_id="loc1", message="Delivered anyway", task_id="unc-2")
        self.bridge.scan(recover=False)
        self.assertEqual(press_calls[0], 1)
        st2 = self.bridge.get_task_status("unc-2")
        self.assertEqual(st2["status"], "in_flight")
        self.assertTrue((self.root / "inbox/in_flight/unc-2.task.json").exists())

    def test_condition3_legacy_pair_uid_none_hub_and_spoke_a_not_eq_b(self) -> None:
        with self.bridge.pairs_tx() as pdata:
            pdata["pairs"]["role_a"] = {
                "pairId": "role_a",
                "label": "Role A",
                "t3ThreadId": "thread-A",
                "defaultReplyThreadId": "thread-B",
                "allowedReplyThreadIds": ["thread-A", "thread-B"],
                "geminiTitle": "Gem-Role-A",
                "route": "gemini",
                "replyMode": "auto",
                "enabled": True,
            }
        self.ax.add_conv("Gem-Role-A")
        tid = "leg-ab-1"
        tb.atomic_json(self.root / "inbox/in_flight" / f"{tid}.task.json", {
            "schemaVersion": 2, "taskId": tid, "pairId": "role_a",
            "replyThreadId": "thread-B", "replyMode": "auto", "message": "Run A->B",
        })
        with self.bridge.ledger_tx() as ld:
            ld["dispatches"][tid] = {
                "status": "in_flight",
                "pairId": "role_a",
                "geminiTitle": "Gem-Role-A",
                "replyThreadId": "thread-B",
                "dispatchedMono": time.monotonic() - 10.0,
                "baselineTextLen": 0,
                "baselineChunks": [],
            }
        self.ax.current_title = "Gem-Role-A"
        self.ax.conversations["Gem-Role-A"]["chunks"].extend([
            "Done from A",
            f"[[T3_BRIDGE_DONE:{tid}]]",
        ])
        self.bridge.scan(recover=False)
        time.sleep(0.25)
        self.bridge.scan(recover=False)
        time.sleep(0.55)
        self.bridge.scan(recover=False)
        self.bridge.scan(recover=False)
        calls = self._cli_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("thread-B", calls[0]["args"])
        self.assertIn("Done from A", calls[0]["message"])

    def test_condition4_reserved_receipt_suffix_and_two_tier_headroom(self) -> None:
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="loc1", adapter="local", t3_thread=None,
                    gemini_title="Gem-Headroom", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
        self.ax.add_conv("Gem-Headroom")

        for bad in ("demo.receipt", "demo.RECEIPT"):
            with self.assertRaises(tb.BridgeError) as cm:
                self.bridge.submit_task(pair_id="loc1", message="bad id", task_id=bad)
            self.assertEqual(str(cm.exception), "reserved_task_id_suffix")

        # Test two-tier headroom: soft intake cap rejects new intake while reserving room for in-flight completion
        res = self.bridge.submit_task(pair_id="loc1", message="Already queued", task_id="hr-1")
        self.bridge.scan(recover=False)
        self.assertEqual(self.bridge.get_task_status(res["taskId"])["status"], "in_flight")

        with mock.patch.object(tb, "LEDGER_INTAKE_MAX_BYTES", 500), mock.patch.object(tb, "LEDGER_HARD_MAX_BYTES", 50000):
            with self.assertRaises(tb.BridgeError) as cm_cap:
                self.bridge.submit_task(pair_id="loc1", message="Should fail soft cap", task_id="hr-2")
            self.assertEqual(str(cm_cap.exception), "ledger_intake_capacity_exceeded")

            # In-flight hr-1 STILL completes cleanly into reserved headroom!
            tb.atomic_json(self.root / "outbox/hr-1.json", {
                "taskId": "hr-1", "pairId": "loc1", "message": "Completed inside headroom",
            })
            self.bridge.scan(recover=False)
            payload = self.bridge.read_task_payload("hr-1")
            self.assertEqual(payload["message"], "Completed inside headroom")

    def test_p0_b_kill_after_ax_submit_and_early_outbox_accepted_in_dispatching(self) -> None:
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="loc1", adapter="local", t3_thread=None,
                    gemini_title="Gem-P0B", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
        self.ax.add_conv("Gem-P0B")
        self.bridge.submit_task(pair_id="loc1", message="Fast outbox", task_id="fast-disp-1")

        # Manually put task into `dispatching` (simulating crash right after AX submit before in_flight move)
        with self.bridge.ledger_tx() as ld:
            ld["dispatches"]["fast-disp-1"]["status"] = "dispatching"
        # Fast outbox arrives while status == dispatching -> accepted cleanly!
        tb.atomic_json(self.root / "outbox/fast-disp-1.json", {
            "taskId": "fast-disp-1", "pairId": "loc1", "message": "Fast reply during dispatching",
        })
        self.bridge.scan(recover=True)
        self.assertEqual(self.bridge.get_task_status("fast-disp-1")["status"], "sent")
        self.assertEqual(self.bridge.read_task_payload("fast-disp-1")["message"], "Fast reply during dispatching")

    def test_p1_1_all_sending_cutpoints_and_direct_t3_interleave(self) -> None:
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="loc1", adapter="local", t3_thread=None,
                    gemini_title="Gem-Loc-Rec", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="t3p", adapter="t3", t3_thread="thread-t3p",
                    gemini_title="Gem-T3-Rec", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
        with tb.file_lock(self.root / ".pairs.lock"):
            loc_uid = self.bridge.pairs()["pairs"]["loc1"]["pairUid"]
            t3_uid = self.bridge.pairs()["pairs"]["t3p"]["pairUid"]

        # Cutpoint (a): local in sending/ BEFORE tasks pre-claim
        msg_a = "Local pre-claim reply"
        tb.atomic_json(self.root / "sending/cut-a-loc.json", {"taskId": "cut-a-loc", "pairId": "loc1", "message": msg_a})
        with self.bridge.ledger_tx() as ld:
            ld["dispatches"]["cut-a-loc"] = {
                "status": "in_flight", "deliveryKind": "gemini_reply", "pairId": "loc1",
                "pairUid": loc_uid, "adapter": "local", "geminiTitle": "Gem-Loc-Rec",
                "replyThreadId": None, "requestSha256": tb.sha256_text("req")[0],
            }

        # Cutpoint (b): already committed sent, sending/ uncleaned
        msg_b = "Already sent"
        sha_b, len_b = tb.sha256_text(msg_b)
        tb.atomic_json(self.root / "sent/payloads/cut-b-loc.json", {"taskId": "cut-b-loc", "pairId": "loc1", "pairUid": loc_uid, "message": msg_b})
        tb.atomic_json(self.root / "sent/cut-b-loc.receipt.json", {"taskId": "cut-b-loc", "accepted": True, "messageSha256": sha_b})
        tb.atomic_json(self.root / "sending/cut-b-loc.json", {"taskId": "cut-b-loc", "pairId": "loc1", "message": msg_b})
        with self.bridge.ledger_tx() as ld:
            ld["tasks"]["cut-b-loc"] = {"status": "sent", "adapter": "local", "pairId": "loc1", "pairUid": loc_uid, "payloadRelPath": "sent/payloads/cut-b-loc.json", "messageSha256": sha_b, "messageBytes": len_b}

        # Cutpoint (c): t3 receipt already persisted before ledger commit -> recovers as sent WITHOUT resending CLI!
        msg_c = "Persisted t3 receipt"
        sha_c, len_c = tb.sha256_text(msg_c)
        tb.atomic_json(self.root / "sent/cut-c-t3.json", {"taskId": "cut-c-t3", "pairId": "t3p", "pairUid": t3_uid, "message": msg_c})
        tb.atomic_json(self.root / "sent/cut-c-t3.receipt.json", {"taskId": "cut-c-t3", "deliveryAdapter": "t3", "accepted": True, "t3": {"threadId": "thread-t3p"}, "messageSha256": sha_c, "messageBytes": len_c})
        tb.atomic_json(self.root / "sending/cut-c-t3.json", {"taskId": "cut-c-t3", "pairId": "t3p", "message": msg_c})
        with self.bridge.ledger_tx() as ld:
            ld["tasks"]["cut-c-t3"] = {"status": "sending", "adapter": "t3", "pairId": "t3p", "pairUid": t3_uid, "resolvedThreadId": "thread-t3p", "messageSha256": sha_c, "messageBytes": len_c}

        self.bridge.scan(recover=True)
        self.assertEqual(self.bridge.read_task_payload("cut-a-loc")["message"], msg_a)
        self.assertEqual(self.bridge.get_task_status("cut-b-loc")["status"], "sent")
        self.assertFalse((self.root / "sending/cut-b-loc.json").exists())
        self.assertEqual(self.bridge.get_task_status("cut-c-t3")["status"], "sent")
        self.assertEqual(len(self._cli_calls()), 0)

        # Test direct_t3_sending live lock & dead caller transition
        live_lock = self.root / "locks/direct_t3.dt3-live.lock"
        fd_live = os.open(str(live_lock), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(fd_live, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.bridge.ledger_tx() as ld:
                ld["dispatches"]["dt3-live"] = {"status": "direct_t3_sending", "deliveryKind": "direct_t3_forwarded", "pairId": "t3p", "pairUid": t3_uid, "adapter": "t3"}
                ld["dispatches"]["dt3-dead"] = {"status": "direct_t3_sending", "deliveryKind": "direct_t3_forwarded", "pairId": "t3p", "pairUid": t3_uid, "adapter": "t3"}
            self.assertEqual(self.bridge.get_task_status("dt3-live")["status"], "direct_t3_sending")
            self.assertEqual(self.bridge.get_task_status("dt3-dead")["status"], "unknown_timeout")
        finally:
            os.close(fd_live)

    def test_p1_2_stage_gates_non_mutation_and_pair_init_none_fix(self) -> None:
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="init", id="loc_init_1", adapter="local", t3_thread=None,
                    gemini_title="Gem-Init-1", reply_to=None, label=None, workspace=None,
                    init_prompt=None, reply_mode="auto", wait_sec=5.0,
                ),
            )
            # Creating 2nd local pair via `pair init` MUST succeed (None == None bug fixed!)
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="init", id="loc_init_2", adapter="local", t3_thread=None,
                    gemini_title="Gem-Init-2", reply_to=None, label=None, workspace=None,
                    init_prompt=None, reply_mode="auto", wait_sec=5.0,
                ),
            )
        # Reject Gemini outbox for queued task and NEVER mutate queued task status or sent task status!
        r = self.bridge.submit_task(pair_id="loc_init_1", message="Still queued", task_id="stage-q-1")
        tb.atomic_json(self.root / "outbox/stage-q-1.json", {"taskId": "stage-q-1", "pairId": "loc_init_1", "message": "Spoof early"})
        self.bridge.scan(recover=False)
        # Quarantine moved bad file to failed/, dispatch remains active/queued (not overwritten!)
        self.assertTrue((self.root / "failed/stage-q-1.json").exists())
        self.assertEqual(self.bridge.get_task_status(r["taskId"])["status"], "in_flight")

    def test_p1_3_flat_t3_counts_plus_2_doctor_and_cli_exit_codes(self) -> None:
        # Doctor with only local pair -> t3CliRequired == False
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="loc1", adapter="local", t3_thread=None,
                    gemini_title="Gem-Loc-Doc", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            tb.cmd_doctor(self.bridge, as_json=True)
        self.assertFalse(json.loads(out.getvalue())["t3CliRequired"])

        # Add enabled t3 pair -> t3CliRequired == True; complete T3 task increments counts["sent"] by +2
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="t3p", adapter="t3", t3_thread="thread-t3",
                    gemini_title="Gem-T3-Flat", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
        with mock.patch("sys.stdout", new=io.StringIO()) as out2:
            tb.cmd_doctor(self.bridge, as_json=True)
        self.assertTrue(json.loads(out2.getvalue())["t3CliRequired"])

        self.ax.add_conv("Gem-T3-Flat")
        self.bridge.submit_task(pair_id="t3p", message="Run T3", task_id="flat-t3-1")
        self.bridge.scan(recover=False)
        tb.atomic_json(self.root / "outbox/flat-t3-1.json", {"taskId": "flat-t3-1", "pairId": "t3p", "message": "Flat reply"})
        self.bridge.scan(recover=False)
        self.assertTrue((self.root / "sent/flat-t3-1.json").exists())
        self.assertTrue((self.root / "sent/flat-t3-1.receipt.json").exists())
        with mock.patch("sys.stdout", new=io.StringIO()) as sout:
            tb.cmd_status(self.bridge, as_json=True)
        self.assertEqual(json.loads(sout.getvalue())["counts"]["sent"], 2)

        # task read works for both t3 and local; mode t3-all rejects local send
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_mode(self.bridge, "t3-all")
        with self.assertRaises(tb.BridgeError) as cm_t3only:
            self.bridge.submit_task(pair_id="loc1", message="Should fail in t3-all")
        self.assertEqual(str(cm_t3only.exception), "gemini_routing_paused_by_global_mode")

    def test_p1_4_unified_needs_attention_card_priority_and_second_card(self) -> None:
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="loc1", adapter="local", t3_thread=None,
                    gemini_title="Gem-Attn-Unified", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
        self.ax.add_conv("Gem-Attn-Unified")
        self.bridge.submit_task(pair_id="loc1", message="Two cards", task_id="attn-u-1")
        self.bridge.scan(recover=False)
        with self.bridge.ledger_tx() as ld:
            ld["dispatches"]["attn-u-1"]["dispatchedMono"] = time.monotonic() - 10.0

        # Same sample has BOTH badge == "in_progress" AND confirmation_card -> card MUST win!
        self.ax.conversations["Gem-Attn-Unified"]["badge"] = "in_progress"
        self.ax.confirmation_card = "ActionCardRemyConfirmationCard"
        self.bridge.scan(recover=False)
        st1 = self.bridge.get_task_status("attn-u-1")
        self.assertEqual(st1["status"], "needs_attention")
        self.assertEqual(st1["attentionEpisode"], 1)

        # CLI task wait --return-on-attention returns exit 3 with original taskId
        rc, out_wait = self.bridge.wait_for_task("attn-u-1", timeout_s=1.0, return_on_attention=True)
        self.assertEqual(rc, 3)
        self.assertEqual(out_wait["taskId"], "attn-u-1")

        # Screen locked MUST NOT clear needs_attention
        self.ax.screen_locked = True
        self.ax.confirmation_card = None
        self.ax.conversations["Gem-Attn-Unified"]["badge"] = "in_progress"
        self.bridge.scan(recover=False)
        self.assertEqual(self.bridge.get_task_status("attn-u-1")["status"], "needs_attention")

        # Unlock + verified card gone -> exits needs_attention -> in_flight, resetting notifiedAttention
        self.ax.screen_locked = False
        self.bridge.scan(recover=False)
        self.assertEqual(self.bridge.get_task_status("attn-u-1")["status"], "in_flight")

        # 2nd card appears -> re-notifies & attentionEpisode == 2
        self.ax.confirmation_card = "ActionCardRemyElicitationCard"
        self.bridge.scan(recover=False)
        st2 = self.bridge.get_task_status("attn-u-1")
        self.assertEqual(st2["status"], "needs_attention")
        self.assertEqual(st2["attentionEpisode"], 2)

    def test_step3_bridge_ax_press_uncertain_vs_not_found(self) -> None:
        bx = bridge_ax.MacOSAXBridge.__new__(bridge_ax.MacOSAXBridge)
        bx.cf = mock.Mock()
        bx.ax = mock.Mock()
        bx.ax.AXUIElementSetAttributeValue.return_value = 0
        bx._k_true = object()
        bx._attr = mock.Mock(side_effect=lambda name: f"attr:{name}")
        bx._cfstr = mock.Mock(side_effect=lambda s: f"cf:{s}")
        bx._app_el = mock.Mock(return_value=101)
        bx._find_main_window = mock.Mock(return_value=202)
        bx._find_composer = mock.Mock(return_value=303)
        bx._get_str = mock.Mock(return_value="Prompt body")
        bx._has_stop_button = mock.Mock(return_value=False)

        # Case 1: button found once and AXPress returns nonzero (-25204) -> submit_press_uncertain, NO retry
        bx._find_element = mock.Mock(return_value=404)
        bx.perform_action = mock.Mock(return_value=-25204)
        ok1, r1 = bx.write_prompt_and_submit(999, "Prompt body")
        self.assertFalse(ok1)
        self.assertEqual(r1, "submit_press_uncertain")
        self.assertEqual(bx.perform_action.call_count, 1)

        # Case 2: button never found -> send_button_not_found_or_disabled, 0 presses
        bx._find_element = mock.Mock(return_value=None)
        bx.perform_action = mock.Mock()
        with (
            mock.patch.object(bridge_ax.time, "monotonic", side_effect=[0.0, 0.1, 2.0]),
            mock.patch.object(bridge_ax.time, "sleep"),
        ):
            ok2, r2 = bx.write_prompt_and_submit(999, "Prompt body")
        self.assertFalse(ok2)
        self.assertEqual(r2, "send_button_not_found_or_disabled")
        self.assertEqual(bx.perform_action.call_count, 0)

    def test_step3_barrier_pause_and_slow_ax_cutover(self) -> None:
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="loc_bar", adapter="local", t3_thread=None,
                    gemini_title="Gem-Barrier", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
        self.ax.add_conv("Gem-Barrier")

        # Verify atomic_rename_excl rejects overwriting existing destination
        existing_dst = self.root / "inbox/existing.task.json"
        tb.atomic_json(existing_dst, {"keep": True})
        src_over = self.root / "inbox/._stage.over.task.json"
        tb.atomic_json(src_over, {"keep": False})
        try:
            with self.assertRaises(FileExistsError):
                tb.atomic_rename_excl(src_over, existing_dst, lock_dir=self.root)
            self.assertTrue(tb.decode_json(existing_dst.read_bytes())["keep"])
        finally:
            existing_dst.unlink()
            if src_over.exists():
                src_over.unlink()

        # Threaded P0-A barrier between Stage 2 (ledger reservation) and Stage 3 (atomic_rename_excl)
        tid = "barrier-p0a-1"
        msg = "Barrier task"
        req_sha, _ = tb.sha256_text(msg)
        stage_name = f"._stage.{tid}.cafebabe.task.json"
        stage_path = self.root / "inbox" / stage_name
        inbox_path = self.root / "inbox" / f"{tid}.task.json"
        stage_ready = threading.Event()
        release_stage = threading.Event()
        prod_err: list[BaseException] = []

        with tb.file_lock(self.root / ".pairs.lock"):
            loc_uid = self.bridge.pairs()["pairs"]["loc_bar"]["pairUid"]

        def producer() -> None:
            try:
                raw_env = json.dumps({
                    "schemaVersion": 2, "taskId": tid, "pairId": "loc_bar", "pairUid": loc_uid,
                    "adapter": "local", "replyThreadId": None, "replyMode": "auto", "message": msg,
                }).encode("utf-8")
                fd = os.open(str(stage_path), os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    os.write(fd, raw_env)
                    os.fsync(fd)
                    with self.bridge.ledger_tx() as ld:
                        ld["dispatches"][tid] = {
                            "status": "queued", "deliveryKind": "gemini_reply", "pairId": "loc_bar",
                            "pairUid": loc_uid, "adapter": "local", "geminiTitle": "Gem-Barrier",
                            "replyThreadId": None, "requestSha256": req_sha,
                            "stagingFile": stage_name, "published": False, "createdAt": tb.now_iso(),
                        }
                    stage_ready.set()
                    release_stage.wait(5.0)
                    tb.atomic_rename_excl(stage_path, inbox_path, lock_dir=self.root)
                    with self.bridge.ledger_tx(enforce_intake_cap=False) as ld:
                        ld["dispatches"][tid]["published"] = True
                finally:
                    os.close(fd)
            except BaseException as exc:
                prod_err.append(exc)
                stage_ready.set()

        th = threading.Thread(target=producer, daemon=True)
        th.start()
        self.assertTrue(stage_ready.wait(3.0))
        # While paused at barrier, continuous scan ignores staging and never creates premature inbox file
        self.bridge.scan(recover=False)
        self.assertFalse(inbox_path.exists())
        self.assertTrue(stage_path.exists())

        release_stage.set()
        th.join(timeout=5.0)
        self.assertEqual(prod_err, [])
        self.assertTrue(inbox_path.exists())
        self.assertFalse(stage_path.exists())
        self.assertEqual(os.stat(inbox_path).st_nlink, 1)

        # Next scan dispatches cleanly
        self.bridge.scan(recover=False)
        self.assertEqual(self.bridge.get_task_status(tid)["status"], "in_flight")

        # P0-1 slow AX submit cutover fence
        import plistlib
        old_script = self.tmp / "old_cutover.py"
        old_script.write_text("#!/usr/bin/python3\n# old v2.0\n", encoding="utf-8")
        new_script = self.tmp / "new_cutover.py"
        new_script.write_text('#!/usr/bin/python3\nBRIDGE_VERSION = "2.1.0"\n', encoding="utf-8")
        plist_file = self.la_dir / "io.test.cutover.plist"
        plist_file.write_bytes(plistlib.dumps({
            "Label": "io.test.cutover",
            "ProgramArguments": ["/usr/bin/python3", str(old_script), "--root", str(self.root), "run"],
            "EnvironmentVariables": {"T3_GEMINI_BRIDGE_ROOT": str(self.root)},
            "KeepAlive": True,
        }))
        tb.atomic_json(self.root / ".daemon_health.json", {
            "schemaVersion": 1, "pid": 42424, "startedAt": tb.now_iso(), "lastScanAt": tb.now_iso(),
            "axTrusted": True, "screenLocked": False, "geminiRunning": True, "geminiPid": 99999,
            "globalMode": "hybrid", "lastError": None,
        })
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_mode(self.bridge, "t3-all")
        self.assertEqual(self.bridge.config()["global_mode"], "t3_only")

        with tb.file_lock(self.root / ".process.lock", nonblocking=True) as lfd:
            os.ftruncate(lfd, 0)
            os.write(lfd, b"42424")
            with mock.patch.object(tb, "agent_info", return_value={"loaded": True, "pid": 42424, "state": "running"}):
                ok_before, _ = self.bridge.check_v21_runtime_for_root(self.bridge.config())
                self.assertFalse(ok_before)
                # Even once slow scan ends and old daemon emits globalMode="t3_only", gate stays closed while old lock/plist remain
                tb.atomic_json(self.root / ".daemon_health.json", {
                    "schemaVersion": 1, "pid": 42424, "startedAt": tb.now_iso(), "lastScanAt": tb.now_iso(),
                    "axTrusted": True, "screenLocked": False, "geminiRunning": True, "geminiPid": 99999,
                    "globalMode": "t3_only", "lastError": None,
                })
                ok_after_hb, _ = self.bridge.check_v21_runtime_for_root(self.bridge.config())
                self.assertFalse(ok_after_hb)

        # Controlled bootout + file swap unlocks v2.1
        plist_file.write_bytes(plistlib.dumps({
            "Label": "io.test.cutover",
            "ProgramArguments": ["/usr/bin/python3", str(new_script), "--root", str(self.root), "run"],
            "EnvironmentVariables": {"T3_GEMINI_BRIDGE_ROOT": str(self.root)},
        }))
        tb.atomic_json(self.root / "t3_gemini_bridge.py", {"dummy": 1})  # ensure replaced cleanly next line
        (self.root / "t3_gemini_bridge.py").write_text('#!/usr/bin/python3\nBRIDGE_VERSION = "2.1.0"\n', encoding="utf-8")
        with mock.patch.object(tb, "agent_info", return_value={"loaded": False}):
            ok_final, reason_final = self.bridge.check_v21_runtime_for_root(self.bridge.config())
            self.assertTrue(ok_final)
            self.assertEqual(reason_final, "offline_upgraded_root")

    def test_step3_legacy_samples_deferral_and_read_errors(self) -> None:
        with self.bridge.pairs_tx() as pdata:
            pdata["pairs"]["t3_leg"] = {
                "pairId": "t3_leg", "label": "Legacy T3", "t3ThreadId": "thread-leg",
                "defaultReplyThreadId": "thread-leg", "allowedReplyThreadIds": ["thread-leg"],
                "geminiTitle": "Gem-Leg-S1", "route": "gemini", "replyMode": "auto", "enabled": True,
            }
        self.ax.add_conv("Gem-Leg-S1")

        # Legacy sample 1: queued inbox file without any dispatches entry
        tid1 = "legacy-no-disp-1"
        tb.atomic_json(self.root / "inbox" / f"{tid1}.task.json", {
            "schemaVersion": 2, "taskId": tid1, "pairId": "t3_leg",
            "replyThreadId": "thread-leg", "replyMode": "auto", "message": "Legacy no snapshot",
        })
        self.bridge.scan(recover=False)
        self.assertEqual(self.bridge.get_task_status(tid1)["status"], "in_flight")
        with tb.file_lock(self.root / ".ledger.lock"):
            self.assertIsNone(self.bridge.ledger()["dispatches"][tid1]["pairUid"])

        # Lazy pairUid deferral while active pairUid=None task exists
        res_def = self.bridge.submit_task(
            pair_id="t3_leg", message="Defer uid for CLI", task_id="legacy-def-2", forbid_legacy_pair_uid_deferral=False
        )
        self.assertIsNone(self.bridge.pairs()["pairs"]["t3_leg"].get("pairUid"))
        with self.assertRaises(tb.BridgeError) as cm_mcp_def:
            self.bridge.submit_task(
                pair_id="t3_leg", message="Reject MCP during active legacy", forbid_legacy_pair_uid_deferral=True
            )
        self.assertEqual(str(cm_mcp_def.exception), "pair_has_active_legacy_task_retry_after_drain")

        # Legacy sample 2: minimal v2 outbox without pairUid delivers cleanly
        tb.atomic_json(self.root / "outbox" / f"{tid1}.json", {
            "taskId": tid1, "pairId": "t3_leg", "message": "Legacy minimal reply",
        })
        self.bridge.scan(recover=False)
        self.assertEqual(self.bridge.get_task_status(tid1)["status"], "sent")
        self.assertEqual(len(self._cli_calls()), 1)

        # Deliver queued legacy-def-2 as well
        self.bridge.scan(recover=False)
        tb.atomic_json(self.root / "outbox/legacy-def-2.json", {
            "taskId": "legacy-def-2", "pairId": "t3_leg", "message": "Deferred reply",
        })
        self.bridge.scan(recover=False)
        self.assertEqual(self.bridge.get_task_status("legacy-def-2")["status"], "sent")

        # Legacy sample 3: flat v1 sent/<tid>.json payload + read error distinctions
        v1_msg = "Flat v1 archived message"
        v1_sha, v1_bytes = tb.sha256_text(v1_msg)
        v1_path = self.root / "sent/legacy-v1-3.json"
        tb.atomic_json(v1_path, {"taskId": "legacy-v1-3", "pairId": "t3_leg", "message": v1_msg})
        bad_msg = "Original message"
        bad_sha, bad_bytes = tb.sha256_text(bad_msg)
        tb.atomic_json(self.root / "sent/payloads/corrupt-4.json", {"taskId": "corrupt-4", "pairId": "t3_leg", "message": "TAMPERED"})
        with self.bridge.ledger_tx() as ld:
            ld["tasks"]["legacy-v1-3"] = {"status": "sent", "adapter": "t3", "pairId": "t3_leg", "messageSha256": v1_sha, "messageBytes": v1_bytes}
            ld["tasks"]["corrupt-4"] = {"status": "sent", "adapter": "t3", "pairId": "t3_leg", "payloadRelPath": "sent/payloads/corrupt-4.json", "messageSha256": bad_sha, "messageBytes": bad_bytes}
            ld["dispatches"]["dt3-only-5"] = {"status": "direct_t3", "deliveryKind": "direct_t3_forwarded", "pairId": "t3_leg", "adapter": "t3"}
        self.assertEqual(self.bridge.read_task_payload("legacy-v1-3")["message"], v1_msg)

        v1_path.unlink()
        with self.assertRaises(tb.BridgeError) as cm_unavail:
            self.bridge.read_task_payload("legacy-v1-3")
        self.assertEqual(str(cm_unavail.exception), "historical_payload_unavailable")

        with self.assertRaises(tb.BridgeError) as cm_corrupt:
            self.bridge.read_task_payload("corrupt-4")
        self.assertEqual(str(cm_corrupt.exception), "archived_payload_corrupted")

        with self.assertRaises(tb.BridgeError) as cm_dt3:
            self.bridge.read_task_payload("dt3-only-5")
        self.assertEqual(str(cm_dt3.exception), "direct_t3_has_no_gemini_reply")

        # pair remove + pair add mints a strictly fresh pairUid
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_pair(self.bridge, argparse.Namespace(pair_cmd="remove", id="t3_leg", force=True))
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="t3_leg", adapter="t3", t3_thread="thread-leg",
                    gemini_title="Gem-Leg-S1", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
            uid1 = self.bridge.pairs()["pairs"]["t3_leg"]["pairUid"]
            tb.cmd_pair(self.bridge, argparse.Namespace(pair_cmd="remove", id="t3_leg", force=True))
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="t3_leg", adapter="t3", t3_thread="thread-leg",
                    gemini_title="Gem-Leg-S1", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
            uid2 = self.bridge.pairs()["pairs"]["t3_leg"]["pairUid"]
        self.assertIsNotNone(uid1)
        self.assertIsNotNone(uid2)
        self.assertNotEqual(uid1, uid2)

        # Interleaved direct_t3 commit under caller lock NEVER downgraded to unknown_timeout
        ilock = self.root / "locks/direct_t3.dt3-inter.lock"
        fd_il = os.open(str(ilock), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(fd_il, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.bridge.ledger_tx() as ld:
                ld["dispatches"]["dt3-inter"] = {"status": "direct_t3_sending", "deliveryKind": "direct_t3_forwarded", "pairId": "t3_leg", "pairUid": uid2, "adapter": "t3"}
            self.assertEqual(self.bridge.get_task_status("dt3-inter")["status"], "direct_t3_sending")
            with self.bridge.ledger_tx() as ld:
                ld["dispatches"]["dt3-inter"]["status"] = "direct_t3"
        finally:
            os.close(fd_il)
        self.assertEqual(self.bridge.get_task_status("dt3-inter")["status"], "direct_t3")

    def test_step3_local_clean_reply_wait_exit_codes_and_outbox_card_timeout(self) -> None:
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="loc_clean", adapter="local", t3_thread=None,
                    gemini_title="Gem-Clean", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="loc_obx", adapter="local", t3_thread=None,
                    gemini_title="Gem-OutboxOnly", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="outbox", force=False,
                ),
            )
        self.ax.add_conv("Gem-Clean")
        self.ax.add_conv("Gem-OutboxOnly")

        # End-to-end local AX capture produces clean reply without [Bridge Reply: ... -> T3]
        self.bridge.submit_task(pair_id="loc_clean", message="Clean Q", task_id="clean-1", idempotency_key="ik-clean-1")
        self.bridge.scan(recover=False)
        with self.bridge.ledger_tx() as ld:
            ld["dispatches"]["clean-1"]["dispatchedMono"] = time.monotonic() - 10.0
        self.ax.current_title = "Gem-Clean"
        self.ax.conversations["Gem-Clean"]["chunks"].extend(["Pure clean answer", "[[T3_BRIDGE_DONE:clean-1]]"])
        self.bridge.scan(recover=False)
        time.sleep(0.25)
        self.bridge.scan(recover=False)
        time.sleep(0.55)
        self.bridge.scan(recover=False)
        self.bridge.scan(recover=False)
        self.assertEqual(self.bridge.get_task_status("clean-1")["status"], "sent")
        clean_msg = self.bridge.read_task_payload("clean-1")["message"]
        self.assertEqual(clean_msg, "Pure clean answer")
        self.assertNotIn("[Bridge Reply:", clean_msg)

        # CLI `send --wait` (via idempotency replay) and `task wait/read` exit codes 0 / 1 / 2 / 3
        with mock.patch("sys.stdout", new=io.StringIO()) as buf_send:
            rc_send = tb.cmd_send(
                self.bridge,
                argparse.Namespace(
                    pair="loc_clean", message="Clean Q", file=None, stdin=False,
                    task_id=None, reply_to=None, idempotency_key="ik-clean-1",
                    wait=True, timeout=1.0, return_on_attention=False,
                ),
            )
        self.assertEqual(rc_send, 0)
        self.assertEqual(json.loads(buf_send.getvalue())["message"], "Pure clean answer")

        with mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(tb.cmd_task(self.bridge, argparse.Namespace(task_cmd="read", task_id="clean-1")), 0)
            self.assertEqual(tb.cmd_task(self.bridge, argparse.Namespace(task_cmd="wait", task_id="clean-1", timeout=0.5, return_on_attention=True)), 0)

            self.bridge.submit_task(pair_id="loc_clean", message="Queued timeout", task_id="q-wait-1")
            self.assertEqual(tb.cmd_task(self.bridge, argparse.Namespace(task_cmd="wait", task_id="q-wait-1", timeout=0.5, return_on_attention=True)), 1)

            with self.bridge.ledger_tx() as ld:
                ld["dispatches"]["fail-wait-2"] = {"status": "failed", "pairId": "loc_clean", "adapter": "local", "reason": "boom"}
                ld["dispatches"]["attn-wait-3"] = {"status": "needs_attention", "pairId": "loc_clean", "adapter": "local", "attentionEpisode": 1}
            self.assertEqual(tb.cmd_task(self.bridge, argparse.Namespace(task_cmd="wait", task_id="fail-wait-2", timeout=0.5, return_on_attention=True)), 2)
            self.assertEqual(tb.cmd_task(self.bridge, argparse.Namespace(task_cmd="wait", task_id="attn-wait-3", timeout=0.5, return_on_attention=True)), 3)

        # replyMode="outbox": confirmation card detected & cleared, NO AX scraping, expires to max_task_wait_exceeded_while_needs_attention
        self.bridge.submit_task(pair_id="loc_obx", message="Outbox mode", task_id="obx-card-1")
        self.bridge.scan(recover=False)
        with self.bridge.ledger_tx() as ld:
            ld["dispatches"]["obx-card-1"]["dispatchedMono"] = time.monotonic() - 10.0

        self.ax.conversations["Gem-OutboxOnly"]["badge"] = "in_progress"
        self.ax.confirmation_card = "ActionCardRemyConfirmationCard"
        self.bridge.scan(recover=False)
        self.assertEqual(self.bridge.get_task_status("obx-card-1")["status"], "needs_attention")

        self.ax.confirmation_card = None
        self.ax.conversations["Gem-OutboxOnly"]["badge"] = None
        self.ax.conversations["Gem-OutboxOnly"]["chunks"].extend(["Should never be AX-scraped", "[[T3_BRIDGE_DONE:obx-card-1]]"])
        self.bridge.scan(recover=False)
        self.assertEqual(self.bridge.get_task_status("obx-card-1")["status"], "in_flight")
        time.sleep(0.25)
        self.bridge.scan(recover=False)
        time.sleep(0.55)
        self.bridge.scan(recover=False)
        self.assertEqual(self.bridge.get_task_status("obx-card-1")["status"], "in_flight")
        self.assertFalse((self.root / "outbox/obx-card-1.json").exists())

        self.ax.confirmation_card = "ActionCardRemyConfirmationCard"
        self.bridge.scan(recover=False)
        self.assertEqual(self.bridge.get_task_status("obx-card-1")["status"], "needs_attention")
        with self.bridge.ledger_tx() as ld:
            ld["dispatches"]["obx-card-1"]["dispatchedMono"] = time.monotonic() - 5000.0
        self.bridge.scan(recover=False)
        st_obx_to = self.bridge.get_task_status("obx-card-1")
        self.assertEqual(st_obx_to["status"], "timed_out")
        self.assertEqual(st_obx_to["reason"], "max_task_wait_exceeded_while_needs_attention")

    def test_review_f1_in_lock_scope_binding_and_v21_gate(self) -> None:
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="loc_f1", adapter="local", t3_thread=None,
                    gemini_title="Gem-F1", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="t3_f1", adapter="t3", t3_thread="thread-f1",
                    gemini_title="Gem-T3-F1", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
        loc_uid = self.bridge.pairs()["pairs"]["loc_f1"]["pairUid"]

        # Simulate concurrent rebind local -> t3 occurring right before submit_task reads pairs() under .pairs.lock
        orig_pairs = self.bridge.pairs

        def rebind_on_read() -> dict:
            pdata = orig_pairs()
            pdata["pairs"]["loc_f1"] = {
                "pairId": "loc_f1", "pairUid": loc_uid, "adapter": "t3", "label": "loc_f1",
                "t3ThreadId": "thread-hijack", "defaultReplyThreadId": "thread-hijack",
                "allowedReplyThreadIds": ["thread-hijack"], "geminiTitle": "Gem-F1",
                "route": "t3", "replyMode": "auto", "enabled": True,
            }
            tb.atomic_json(self.root / "pairs.json", pdata)
            return orig_pairs()

        with mock.patch.object(self.bridge, "pairs", side_effect=rebind_on_read):
            with self.assertRaises(tb.BridgeError) as cm:
                self.bridge.submit_task(
                    pair_id="loc_f1", message="Must fail inside lock",
                    allowed_adapters={"local"}, allow_pairs={"loc_f1"},
                    expected_pair_uid=loc_uid, expected_adapter="local",
                    expected_gemini_title="Gem-F1", require_v21_runtime=True,
                )
            self.assertEqual(str(cm.exception), "pair_not_accessible_in_scope")
        self.assertEqual(self._cli_calls(), [])
        self.assertEqual(list((self.root / "inbox").glob("*.task.json")), [])

        # Also reject stale pairUid or changed geminiTitle inside lock
        t3_uid = self.bridge.pairs()["pairs"]["t3_f1"]["pairUid"]
        with self.assertRaises(tb.BridgeError) as cm_uid:
            self.bridge.submit_task(
                pair_id="t3_f1", message="Bad uid",
                allowed_adapters={"t3"}, expected_pair_uid="f" * 32, require_v21_runtime=True,
            )
        self.assertEqual(str(cm_uid.exception), "pair_not_accessible_in_scope")
        self.assertNotEqual(t3_uid, "f" * 32)

        # Require v2.1 runtime rejects sliding into v2.0 compat when old daemon holds .process.lock
        tb.atomic_json(self.root / ".daemon_health.json", {
            "schemaVersion": 1, "pid": 42424, "startedAt": tb.now_iso(), "lastScanAt": tb.now_iso(),
            "axTrusted": True, "screenLocked": False, "geminiRunning": True, "geminiPid": 99999,
            "globalMode": "hybrid", "lastError": None,
        })
        with tb.file_lock(self.root / ".process.lock", nonblocking=True) as fd:
            os.ftruncate(fd, 0)
            os.write(fd, b"42424")
            with self.assertRaises(tb.BridgeError) as cm_gate:
                self.bridge.submit_task(
                    pair_id="t3_f1", message="Reject compat",
                    allowed_adapters={"t3"}, require_v21_runtime=True,
                )
            self.assertEqual(str(cm_gate.exception), "daemon_upgrade_required_for_v21_features")
        self.assertEqual(list((self.root / "inbox").glob("*.task.json")), [])

    def test_review_f2_publish_reconcile_interleave_never_marks_missing(self) -> None:
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="loc_f2", adapter="local", t3_thread=None,
                    gemini_title="Gem-F2", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
        self.ax.add_conv("Gem-F2")
        loc_uid = self.bridge.pairs()["pairs"]["loc_f2"]["pairUid"]

        tid = "f2-race-1"
        msg = "Interleaved stage3 rename"
        req_sha, _ = tb.sha256_text(msg)
        stage_name = f"._stage.{tid}.12345678.task.json"
        stage_path = self.root / "inbox" / stage_name
        inbox_path = self.root / "inbox" / f"{tid}.task.json"
        tb.atomic_json(stage_path, {
            "schemaVersion": 2, "taskId": tid, "pairId": "loc_f2", "pairUid": loc_uid,
            "adapter": "local", "replyThreadId": None, "replyMode": "auto", "message": msg, "createdAt": tb.now_iso(),
        })
        with self.bridge.ledger_tx() as ld:
            ld["dispatches"][tid] = {
                "status": "queued", "deliveryKind": "gemini_reply", "pairId": "loc_f2",
                "pairUid": loc_uid, "adapter": "local", "geminiTitle": "Gem-F2",
                "replyThreadId": None, "requestSha256": req_sha,
                "stagingFile": stage_name, "published": False, "createdAt": tb.now_iso(),
            }

        # Race producer atomic_rename_excl RIGHT WHEN reconcile_staged_intake calls os.open(stage_path) -> triggers ENOENT!
        orig_os_open = tb.os.open
        raced = [False]

        def race_os_open(path_str: str, flags: int, *args, **kwargs):
            if not raced[0] and path_str == str(stage_path):
                raced[0] = True
                tb.atomic_rename_excl(stage_path, inbox_path, lock_dir=self.root)
            return orig_os_open(path_str, flags, *args, **kwargs)

        with mock.patch.object(tb.os, "open", side_effect=race_os_open):
            self.bridge.reconcile_staged_intake(self.bridge.config())

        self.assertTrue(raced[0])
        self.assertTrue(inbox_path.exists())
        self.assertEqual(os.stat(inbox_path).st_nlink, 1)
        with tb.file_lock(self.root / ".ledger.lock"):
            d_rec = self.bridge.ledger()["dispatches"][tid]
        self.assertEqual(d_rec["status"], "queued")
        self.assertTrue(d_rec["published"])

        self.bridge.scan(recover=False)
        self.assertEqual(self.bridge.get_task_status(tid)["status"], "in_flight")

    def test_review_f3_legacy_in_flight_without_dispatch_delivers_reply(self) -> None:
        with self.bridge.pairs_tx() as pdata:
            pdata["pairs"]["t3_f3"] = {
                "pairId": "t3_f3", "label": "Legacy F3", "t3ThreadId": "thread-A",
                "defaultReplyThreadId": "thread-B", "allowedReplyThreadIds": ["thread-A", "thread-B"],
                "geminiTitle": "Gem-F3", "route": "gemini", "replyMode": "auto", "enabled": True,
            }
        self.ax.add_conv("Gem-F3")

        tid = "leg-inflight-3"
        tb.atomic_json(self.root / "inbox/in_flight" / f"{tid}.task.json", {
            "schemaVersion": 2, "taskId": tid, "pairId": "t3_f3",
            "replyThreadId": "thread-B", "replyMode": "auto", "message": "In-flight without ledger",
        })
        # Run scan BEFORE outbox arrives -> hydrates COMPLETE dispatch instead of hollow dispatchedMono
        self.bridge.scan(recover=False)
        with tb.file_lock(self.root / ".ledger.lock"):
            d_rec = self.bridge.ledger()["dispatches"].get(tid)
        self.assertIsNotNone(d_rec)
        self.assertEqual(d_rec["status"], "in_flight")
        self.assertEqual(d_rec["pairId"], "t3_f3")
        self.assertIsNone(d_rec["pairUid"])
        self.assertEqual(d_rec["replyThreadId"], "thread-B")

        # Valid legacy v2 outbox arrives -> delivered to thread-B cleanly, 0 failed files!
        tb.atomic_json(self.root / "outbox" / f"{tid}.json", {
            "taskId": tid, "pairId": "t3_f3", "message": "Legacy in_flight answer",
        })
        self.bridge.scan(recover=False)
        self.assertEqual(list((self.root / "failed").iterdir()), [])
        self.assertEqual(self.bridge.get_task_status(tid)["status"], "sent")
        calls = self._cli_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("thread-B", calls[0]["args"])

    def test_review_f4_legacy_queued_envelope_never_gains_pair_uid(self) -> None:
        with self.bridge.pairs_tx() as pdata:
            pdata["pairs"]["t3_f4"] = {
                "pairId": "t3_f4", "label": "Legacy F4", "t3ThreadId": "thread-f4",
                "defaultReplyThreadId": "thread-f4", "allowedReplyThreadIds": ["thread-f4"],
                "geminiTitle": "Gem-F4", "route": "gemini", "replyMode": "auto", "enabled": True,
            }
        self.ax.add_conv("Gem-F4")

        # 1. Unregistered v2.0 queued envelope arrives FIRST (no ledger entry)
        old_tid = "001-old-queued-4"
        tb.atomic_json(self.root / "inbox" / f"{old_tid}.task.json", {
            "schemaVersion": 2, "taskId": old_tid, "pairId": "t3_f4",
            "replyThreadId": "thread-f4", "replyMode": "auto", "message": "Legacy queued first",
        })

        # 2. New CLI task arrives SECOND -> lazy pairUid MUST stay None! And MCP rejects until drained.
        res_cli = self.bridge.submit_task(
            pair_id="t3_f4", message="CLI after legacy queued", task_id="002-cli-after-4", forbid_legacy_pair_uid_deferral=False
        )
        self.assertIsNone(self.bridge.pairs()["pairs"]["t3_f4"].get("pairUid"))
        self.assertIsNone(res_cli["pairUid"])
        with self.assertRaises(tb.BridgeError) as cm_mcp:
            self.bridge.submit_task(
                pair_id="t3_f4", message="MCP must wait", forbid_legacy_pair_uid_deferral=True
            )
        self.assertEqual(str(cm_mcp.exception), "pair_has_active_legacy_task_retry_after_drain")

        # 3. Daemon consumes 001-old-queued-4 first -> pairUid stays None on its dispatch record
        self.bridge.scan(recover=False)
        with tb.file_lock(self.root / ".ledger.lock"):
            self.assertIsNone(self.bridge.ledger()["dispatches"][old_tid]["pairUid"])

        # Complete both active tasks so pair drains cleanly
        for tid_done in (old_tid, "002-cli-after-4"):
            self.bridge.scan(recover=False)
            tb.atomic_json(self.root / "outbox" / f"{tid_done}.json", {
                "taskId": tid_done, "pairId": "t3_f4", "message": f"Done {tid_done}",
            })
            self.bridge.scan(recover=False)

        # 4. Now submit new v2.1 task (mints pairUid), then drop a late unreserved legacy envelope -> MUST STILL get pairUid=None!
        res_new = self.bridge.submit_task(pair_id="t3_f4", message="Mint UID now", task_id="mint-4")
        self.assertIsNotNone(res_new["pairUid"])
        self.bridge.scan(recover=False)
        tb.atomic_json(self.root / "outbox/mint-4.json", {"taskId": "mint-4", "pairId": "t3_f4", "message": "Done mint-4"})
        self.bridge.scan(recover=False)

        late_tid = "late-legacy-4"
        tb.atomic_json(self.root / "inbox" / f"{late_tid}.task.json", {
            "schemaVersion": 2, "taskId": late_tid, "pairId": "t3_f4",
            "replyThreadId": "thread-f4", "replyMode": "auto", "message": "Late legacy envelope",
        })
        self.bridge.scan(recover=False)
        with tb.file_lock(self.root / ".ledger.lock"):
            self.assertIsNone(self.bridge.ledger()["dispatches"][late_tid]["pairUid"])

    def test_review_f5_offline_gate_accepts_legacy_filename_and_rejects_large_v20(self) -> None:
        import plistlib
        real_src = SCRIPTS_DIR / "gemini_task_bridge.py"
        installed_dst = self.root / "t3_gemini_bridge.py"
        shutil.copy2(real_src, installed_dst)
        self.assertGreater(installed_dst.stat().st_size, 100_000)

        plist_file = self.la_dir / "io.test.realscript.plist"
        plist_file.write_bytes(plistlib.dumps({
            "Label": "io.test.realscript",
            "ProgramArguments": ["/usr/bin/python3", str(installed_dst), "--root", str(self.root), "run"],
            "EnvironmentVariables": {"T3_GEMINI_BRIDGE_ROOT": str(self.root)},
            "KeepAlive": True,
        }))

        self.assertEqual(tb._script_bridge_version(installed_dst), (2, 2, 0))
        with mock.patch.object(tb, "agent_info", return_value={"loaded": False}):
            ok_v21, reason_v21 = self.bridge.check_v21_runtime_for_root(self.bridge.config())
            self.assertTrue(ok_v21)
            self.assertEqual(reason_v21, "offline_upgraded_root")

        # Large >100 KiB script WITHOUT BRIDGE_VERSION parses as (2, 0, 0) and is rejected!
        v20_dir = self.tmp / "v20_dir"
        v20_dir.mkdir(parents=True)
        large_v20 = v20_dir / "t3_gemini_bridge.py"
        large_v20.write_text("#!/usr/bin/python3\n" + ("# v2.0 padding line\n" * 6000), encoding="utf-8")
        self.assertGreater(large_v20.stat().st_size, 100_000)
        plist_file.write_bytes(plistlib.dumps({
            "Label": "io.test.realscript",
            "ProgramArguments": ["/usr/bin/python3", str(large_v20), "--root", str(self.root), "run"],
            "EnvironmentVariables": {"T3_GEMINI_BRIDGE_ROOT": str(self.root)},
            "KeepAlive": True,
        }))
        self.assertEqual(tb._script_bridge_version(large_v20), (2, 0, 0))
        with mock.patch.object(tb, "agent_info", return_value={"loaded": False}):
            ok_v20, reason_v20 = self.bridge.check_v21_runtime_for_root(self.bridge.config())
            self.assertFalse(ok_v20)
            self.assertEqual(reason_v20, "daemon_upgrade_required_for_v21_features")

    def test_review_f7_and_f8_legacy_receipt_readback_corrupt_wait_and_sending_hydration(self) -> None:
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="loc_f78", adapter="local", t3_thread=None,
                    gemini_title="Gem-F78", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
        loc_uid = self.bridge.pairs()["pairs"]["loc_f78"]["pairUid"]

        # F7: legacy `old.receipt` flat sent/<tid>.json (no payloadRelPath) reads back cleanly while new intake stays blocked
        r_msg = "Legacy .receipt reply body"
        r_sha, r_bytes = tb.sha256_text(r_msg)
        tb.atomic_json(self.root / "sent/old.receipt.json", {"taskId": "old.receipt", "pairId": "loc_f78", "message": r_msg})
        tb.atomic_json(self.root / "sent/old.receipt.receipt.json", {"taskId": "old.receipt", "accepted": True, "messageSha256": r_sha})
        tb.atomic_json(self.root / "sent/bad.receipt.json", {"taskId": "bad.receipt", "accepted": True})
        with self.bridge.ledger_tx() as ld:
            ld["tasks"]["old.receipt"] = {"status": "sent", "adapter": "t3", "pairId": "loc_f78", "messageSha256": r_sha, "messageBytes": r_bytes}
            ld["tasks"]["bad.receipt"] = {"status": "sent", "adapter": "t3", "pairId": "loc_f78"}
        self.assertEqual(self.bridge.read_task_payload("old.receipt")["message"], r_msg)
        with self.assertRaises(tb.BridgeError) as cm_rec:
            self.bridge.read_task_payload("bad.receipt")
        self.assertEqual(str(cm_rec.exception), "archived_payload_corrupted")
        with self.assertRaises(tb.BridgeError) as cm_blk:
            self.bridge.submit_task(pair_id="loc_f78", message="New blocked", task_id="old.receipt")
        self.assertEqual(str(cm_blk.exception), "reserved_task_id_suffix")

        # F8a: v2.1 sent task with corrupted archived payload causes wait_for_task -> rc == 2 + error
        c_tid = "v21-corrupt-wait"
        c_orig = "Valid archived text"
        c_sha, c_bytes = tb.sha256_text(c_orig)
        tb.atomic_json(self.root / "sent/payloads" / f"{c_tid}.json", {
            "taskId": c_tid, "pairId": "loc_f78", "pairUid": loc_uid, "message": "CORRUPTED TEXT",
        })
        with self.bridge.ledger_tx() as ld:
            ld["tasks"][c_tid] = {
                "status": "sent", "adapter": "local", "pairId": "loc_f78", "pairUid": loc_uid,
                "payloadRelPath": f"sent/payloads/{c_tid}.json",
                "messageSha256": c_sha, "messageBytes": c_bytes,
            }
            ld["dispatches"][c_tid] = {"status": "completed", "adapter": "local", "pairId": "loc_f78", "pairUid": loc_uid}
        rc_wait_bad, out_wait_bad = self.bridge.wait_for_task(c_tid, timeout_s=0.5)
        self.assertEqual(rc_wait_bad, 2)
        self.assertFalse(out_wait_bad["ok"])
        self.assertEqual(out_wait_bad["error"], "archived_payload_corrupted")

        # F8b: Cutpoint (b) verifies archive BEFORE unlinking sending/, and re-hydrates corrupted archive from sending/
        h_tid = "cutb-hydrate-1"
        h_msg = "Intact sending payload recovers corrupt archive"
        h_sha, h_bytes = tb.sha256_text(h_msg)
        tb.atomic_json(self.root / "sent/payloads" / f"{h_tid}.json", {
            "taskId": h_tid, "pairId": "loc_f78", "pairUid": loc_uid, "message": "BROKEN",
        })
        tb.atomic_json(self.root / "sent" / f"{h_tid}.receipt.json", {
            "taskId": h_tid, "accepted": True, "messageSha256": h_sha, "messageBytes": h_bytes,
        })
        tb.atomic_json(self.root / "sending" / f"{h_tid}.json", {
            "taskId": h_tid, "pairId": "loc_f78", "pairUid": loc_uid, "message": h_msg,
        })
        with self.bridge.ledger_tx() as ld:
            ld["tasks"][h_tid] = {
                "status": "sent", "adapter": "local", "pairId": "loc_f78", "pairUid": loc_uid,
                "payloadRelPath": f"sent/payloads/{h_tid}.json",
                "messageSha256": h_sha, "messageBytes": h_bytes,
            }
            ld["dispatches"][h_tid] = {"status": "completed", "adapter": "local", "pairId": "loc_f78", "pairUid": loc_uid}

        self.bridge.scan(recover=True)
        self.assertFalse((self.root / "sending" / f"{h_tid}.json").exists())
        self.assertEqual(self.bridge.read_task_payload(h_tid)["message"], h_msg)

    def test_review2_p1_legacy_in_flight_unknown_baseline_never_scrapes_prior_turn(self) -> None:
        with self.bridge.pairs_tx() as pdata:
            pdata["pairs"]["t3_r2_a"] = {
                "pairId": "t3_r2_a", "label": "Legacy R2A", "t3ThreadId": "thread-r2a",
                "defaultReplyThreadId": "thread-r2a", "allowedReplyThreadIds": ["thread-r2a"],
                "geminiTitle": "Gem-R2A", "route": "gemini", "replyMode": "auto", "enabled": True,
            }
        self.ax.add_conv("Gem-R2A")
        self.ax.current_title = "Gem-R2A"
        self.ax.conversations["Gem-R2A"]["chunks"].append(
            "Previous turn final answer that is definitely longer than 8 characters and looks completely stable"
        )

        # Hydrated legacy in_flight with NO start/done boundary & NO outbox must NOT deliver prior turn!
        tid1 = "r2a-leg-1"
        tb.atomic_json(self.root / "inbox/in_flight" / f"{tid1}.task.json", {
            "schemaVersion": 2, "taskId": tid1, "pairId": "t3_r2_a",
            "replyThreadId": "thread-r2a", "replyMode": "auto", "message": "Followup without sentinel",
        })
        self.bridge.scan(recover=False)
        with self.bridge.ledger_tx() as ld:
            self.assertTrue(ld["dispatches"][tid1]["baselineUnknown"])
            ld["dispatches"][tid1]["dispatchedMono"] = time.monotonic() - 10.0

        self.bridge.scan(recover=False)
        time.sleep(0.25)
        self.bridge.scan(recover=False)
        time.sleep(0.25)
        self.bridge.scan(recover=False)
        self.bridge.scan(recover=False)
        self.assertEqual(self._cli_calls(), [])
        self.assertFalse((self.root / "outbox" / f"{tid1}.json").exists())
        self.assertEqual(self.bridge.get_task_status(tid1)["status"], "in_flight")

        # Valid outbox still delivers cleanly for the hydrated task
        tb.atomic_json(self.root / "outbox" / f"{tid1}.json", {
            "taskId": tid1, "pairId": "t3_r2_a", "message": "Real outbox reply for r2a",
        })
        self.bridge.scan(recover=False)
        self.assertEqual(self.bridge.get_task_status(tid1)["status"], "sent")
        self.assertEqual(len(self._cli_calls()), 1)
        self.assertIn("Real outbox reply for r2a", self._cli_calls()[0]["message"])

        # Case 2: hydrated legacy in_flight WITH BOTH START and DONE boundaries delivers ONLY new text!
        tid2 = "r2a-leg-2"
        tb.atomic_json(self.root / "inbox/in_flight" / f"{tid2}.task.json", {
            "schemaVersion": 2, "taskId": tid2, "pairId": "t3_r2_a",
            "replyThreadId": "thread-r2a", "replyMode": "auto", "message": "With start and done",
        })
        self.bridge.scan(recover=False)
        with self.bridge.ledger_tx() as ld:
            ld["dispatches"][tid2]["dispatchedMono"] = time.monotonic() - 10.0
        self.ax.conversations["Gem-R2A"]["chunks"].extend([
            f"[[T3_BRIDGE_START:{tid2}]]\n--- TASK ---\nWith start and done",
            "Bounded new reply only",
            f"[[T3_BRIDGE_DONE:{tid2}]]",
        ])
        self.bridge.scan(recover=False)
        time.sleep(0.25)
        self.bridge.scan(recover=False)
        time.sleep(0.55)
        self.bridge.scan(recover=False)
        self.bridge.scan(recover=False)
        self.assertEqual(self.bridge.get_task_status(tid2)["status"], "sent")
        calls = self._cli_calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("Bounded new reply only", calls[1]["message"])
        self.assertNotIn("Previous turn final answer", calls[1]["message"])

    def test_review2_p1_legacy_t3_none_uid_rebind_rejected_in_lock(self) -> None:
        with self.bridge.pairs_tx() as pdata:
            pdata["pairs"]["t3_r2_b"] = {
                "pairId": "t3_r2_b", "label": "Legacy R2B", "t3ThreadId": "thread-OLD",
                "defaultReplyThreadId": "thread-OLD", "allowedReplyThreadIds": ["thread-OLD"],
                "geminiTitle": "Gem-Same", "route": "gemini", "replyMode": "auto", "enabled": True,
            }

        orig_pairs = self.bridge.pairs

        def swap_t3_thread_only() -> dict:
            pdata = orig_pairs()
            pdata["pairs"]["t3_r2_b"]["t3ThreadId"] = "thread-NEW"
            pdata["pairs"]["t3_r2_b"]["defaultReplyThreadId"] = "thread-NEW"
            pdata["pairs"]["t3_r2_b"]["allowedReplyThreadIds"] = ["thread-NEW"]
            tb.atomic_json(self.root / "pairs.json", pdata)
            return orig_pairs()

        with mock.patch.object(self.bridge, "pairs", side_effect=swap_t3_thread_only):
            with self.assertRaises(tb.BridgeError) as cm1:
                self.bridge.submit_task(
                    pair_id="t3_r2_b", message="Reject thread swap",
                    allowed_adapters={"t3"}, expected_pair_uid=None,
                    expected_adapter="t3", expected_gemini_title="Gem-Same",
                    expected_t3_thread_id="thread-OLD", expected_default_reply_thread_id="thread-OLD",
                    require_v21_runtime=True,
                )
            self.assertEqual(str(cm1.exception), "pair_not_accessible_in_scope")
        self.assertEqual(self._cli_calls(), [])
        self.assertEqual(list((self.root / "inbox").glob("*.task.json")), [])
        self.assertIsNone(self.bridge.pairs()["pairs"]["t3_r2_b"].get("pairUid"))

        # Case 2: None -> new pairUid swap before lock ALSO rejected when expected_pair_uid=None
        def swap_none_to_uid() -> dict:
            pdata = orig_pairs()
            pdata["pairs"]["t3_r2_b"]["t3ThreadId"] = "thread-OLD"
            pdata["pairs"]["t3_r2_b"]["defaultReplyThreadId"] = "thread-OLD"
            pdata["pairs"]["t3_r2_b"]["pairUid"] = "9" * 32
            tb.atomic_json(self.root / "pairs.json", pdata)
            return orig_pairs()

        with mock.patch.object(self.bridge, "pairs", side_effect=swap_none_to_uid):
            with self.assertRaises(tb.BridgeError) as cm2:
                self.bridge.submit_task(
                    pair_id="t3_r2_b", message="Reject None->UID swap",
                    allowed_adapters={"t3"}, expected_pair_uid=None,
                    expected_adapter="t3", expected_gemini_title="Gem-Same",
                    require_v21_runtime=True,
                )
            self.assertEqual(str(cm2.exception), "pair_not_accessible_in_scope")
        self.assertEqual(list((self.root / "inbox").glob("*.task.json")), [])

    def test_review2_p1_max_inbox_bytes_precheck_and_queued_failure_settlement(self) -> None:
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="loc_r2_c", adapter="local", t3_thread=None,
                    gemini_title="Gem-R2C", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
        self.ax.add_conv("Gem-R2C")

        # 1. Pre-check rejects serialized envelope > max_inbox_bytes BEFORE reservation/staging
        cfg = self.bridge.config()
        cfg["max_inbox_bytes"] = 512
        self.bridge.save_config(cfg)
        with self.assertRaises(tb.BridgeError) as cm_env:
            self.bridge.submit_task(pair_id="loc_r2_c", message="X" * 460, task_id="too-big-pre")
        self.assertEqual(str(cm_env.exception), "inbox_envelope_limit_exceeded")
        self.assertEqual(list((self.root / "inbox").glob("*.task.json")), [])
        self.assertEqual(list((self.root / "inbox").glob("._stage.*")), [])
        self.assertEqual(self.bridge.get_task_status("too-big-pre")["status"], "not_found")

        # 2. Accepted under large cap, then cap shrunk below file size -> scan moves to failed/ AND settles queued dispatch!
        cfg["max_inbox_bytes"] = 262144
        self.bridge.save_config(cfg)
        self.bridge.submit_task(pair_id="loc_r2_c", message="Y" * 460, task_id="shrink-later-1")
        self.assertEqual(self.bridge.get_task_status("shrink-later-1")["status"], "queued")

        cfg["max_inbox_bytes"] = 512
        self.bridge.save_config(cfg)
        self.bridge.scan(recover=False)
        self.assertFalse((self.root / "inbox/shrink-later-1.task.json").exists())
        self.assertTrue((self.root / "failed/shrink-later-1.task.json").exists())
        st_shrunk = self.bridge.get_task_status("shrink-later-1")
        self.assertEqual(st_shrunk["status"], "failed")
        self.assertTrue(st_shrunk["terminal"])
        self.assertEqual(st_shrunk["reason"], "file_too_large")

        # 3. Oversized bogus inbox file matching an ALREADY in_flight task must NOT clobber its active dispatch status!
        cfg["max_inbox_bytes"] = 262144
        self.bridge.save_config(cfg)
        self.bridge.submit_task(pair_id="loc_r2_c", message="Active ok", task_id="active-r2c-1")
        self.bridge.scan(recover=False)
        self.assertEqual(self.bridge.get_task_status("active-r2c-1")["status"], "in_flight")
        (self.root / "inbox/active-r2c-1.task.json").write_bytes(b"{" + (b"z" * (300 * 1024)) + b"}")
        self.bridge.scan(recover=False)
        self.assertTrue((self.root / "failed/active-r2c-1.task.json").exists())
        self.assertEqual(self.bridge.get_task_status("active-r2c-1")["status"], "in_flight")

    def test_review2_p2_cutpoint_b_receipt_digest_mismatch_restores_original_metadata(self) -> None:
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="t3_r2_d", adapter="t3", t3_thread="thread-r2d",
                    gemini_title="Gem-R2D", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
        t3_uid = self.bridge.pairs()["pairs"]["t3_r2_d"]["pairUid"]
        tid = "cutb-meta-1"
        h_msg = "Original T3 reply content"
        h_sha, h_bytes = tb.sha256_text(h_msg)
        orig_receipt = {
            "schemaVersion": 2, "taskId": tid, "role": "t3_r2_d", "target": "t3_r2_d",
            "pairId": "t3_r2_d", "pairUid": t3_uid, "deliveryAdapter": "t3",
            "resolvedThreadId": "thread-r2d", "captureChannel": "outbox_json",
            "payloadRelPath": f"sent/payloads/{tid}.json", "receiptRelPath": f"sent/{tid}.receipt.json",
            "accepted": True, "acceptedAt": "2026-01-02T03:04:05+00:00",
            "messageBytes": h_bytes, "messageSha256": h_sha,
            "cliExitCode": 0, "cliDurationMs": 123.4, "t3": {"runId": "orig-run"},
        }
        tb.atomic_json(self.root / "sent/payloads" / f"{tid}.json", {
            "taskId": tid, "pairId": "t3_r2_d", "pairUid": t3_uid, "message": h_msg,
        })
        tb.atomic_json(self.root / "sent" / f"{tid}.receipt.json", {
            **orig_receipt, "messageSha256": "deadbeef" * 8,
        })
        tb.atomic_json(self.root / "sending" / f"{tid}.json", {
            "taskId": tid, "pairId": "t3_r2_d", "pairUid": t3_uid, "message": h_msg,
        })
        with self.bridge.ledger_tx() as ld:
            ld["tasks"][tid] = {
                "status": "sent", "adapter": "t3", "pairId": "t3_r2_d", "pairUid": t3_uid,
                "resolvedThreadId": "thread-r2d", "payloadRelPath": f"sent/payloads/{tid}.json",
                "receiptRelPath": f"sent/{tid}.receipt.json",
                "messageSha256": h_sha, "messageBytes": h_bytes, "receipt": orig_receipt,
            }
            ld["dispatches"][tid] = {
                "status": "completed", "adapter": "t3", "pairId": "t3_r2_d", "pairUid": t3_uid,
            }

        self.bridge.scan(recover=True)
        self.assertFalse((self.root / "sending" / f"{tid}.json").exists())
        disk_rec = tb.decode_json((self.root / "sent" / f"{tid}.receipt.json").read_bytes())
        self.assertEqual(disk_rec["messageSha256"], h_sha)
        self.assertEqual(disk_rec["acceptedAt"], "2026-01-02T03:04:05+00:00")
        self.assertEqual(disk_rec["cliDurationMs"], 123.4)
        self.assertEqual(disk_rec["t3"], {"runId": "orig-run"})
        self.assertEqual(self.bridge.read_task_payload(tid)["message"], h_msg)

    def test_review3_p1_noncanonical_payload_relpath_blocked_and_keeps_sending(self) -> None:
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="loc_r3_path", adapter="local", t3_thread=None,
                    gemini_title="Gem-R3Path", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
        loc_uid = self.bridge.pairs()["pairs"]["loc_r3_path"]["pairUid"]
        tid = "noncanon-cutb-1"
        msg = "Must not write outside sent/"
        sha_m, bytes_m = tb.sha256_text(msg)
        tb.atomic_json(self.root / "sending" / f"{tid}.json", {
            "taskId": tid, "pairId": "loc_r3_path", "pairUid": loc_uid, "message": msg,
        })
        tb.atomic_json(self.root / "sent" / f"{tid}.receipt.json", {
            "schemaVersion": 2, "taskId": tid, "accepted": True, "deliveryAdapter": "local",
            "messageSha256": sha_m, "messageBytes": bytes_m,
        })
        with self.bridge.ledger_tx() as ld:
            ld["tasks"][tid] = {
                "status": "sent", "adapter": "local", "pairId": "loc_r3_path", "pairUid": loc_uid,
                "payloadRelPath": "../escape.json",
                "messageSha256": sha_m, "messageBytes": bytes_m,
            }
            ld["dispatches"][tid] = {
                "status": "completed", "adapter": "local", "pairId": "loc_r3_path", "pairUid": loc_uid,
            }

        self.bridge.scan(recover=True)
        self.assertFalse((self.root.parent / "escape.json").exists())
        self.assertTrue((self.root / "sending" / f"{tid}.json").exists())
        with self.assertRaises(tb.BridgeError) as cm_read:
            self.bridge.read_task_payload(tid)
        self.assertEqual(str(cm_read.exception), "archived_payload_corrupted")

    def test_review3_p1_sent_reply_reads_and_waits_despite_small_max_inbox_bytes(self) -> None:
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="loc_r3_size", adapter="local", t3_thread=None,
                    gemini_title="Gem-R3Size", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
        self.ax.add_conv("Gem-R3Size")
        cfg = self.bridge.config()
        cfg["max_inbox_bytes"] = 512
        self.bridge.save_config(cfg)

        self.bridge.submit_task(pair_id="loc_r3_size", message="Short prompt", task_id="small-in-big-out")
        self.bridge.scan(recover=False)
        self.assertEqual(self.bridge.get_task_status("small-in-big-out")["status"], "in_flight")

        big_reply = "R" * 700
        tb.atomic_json(self.root / "outbox/small-in-big-out.json", {
            "taskId": "small-in-big-out", "pairId": "loc_r3_size", "message": big_reply,
        })
        self.bridge.scan(recover=False)
        self.assertEqual(self.bridge.read_task_payload("small-in-big-out")["message"], big_reply)
        rc_w, out_w = self.bridge.wait_for_task("small-in-big-out", timeout_s=1.0)
        self.assertEqual(rc_w, 0)
        self.assertTrue(out_w["ok"])
        self.assertEqual(out_w["message"], big_reply)

    def test_review3_p1_in_flight_survives_lowered_max_inbox_bytes_and_missing_file(self) -> None:
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="loc_r3_if", adapter="local", t3_thread=None,
                    gemini_title="Gem-R3IF", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="loc_r3_miss", adapter="local", t3_thread=None,
                    gemini_title="Gem-R3Miss", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
        self.ax.add_conv("Gem-R3IF")
        self.ax.add_conv("Gem-R3Miss")

        # 1. Start 460-byte task in_flight (> 512-byte envelope), then shrink max_inbox_bytes = 512
        self.bridge.submit_task(pair_id="loc_r3_if", message="P" * 460, task_id="if-shrink-1")
        self.bridge.scan(recover=False)
        self.assertEqual(self.bridge.get_task_status("if-shrink-1")["status"], "in_flight")
        in_fl_file = self.root / "inbox/in_flight/if-shrink-1.task.json"
        self.assertGreater(in_fl_file.stat().st_size, 512)

        cfg = self.bridge.config()
        cfg["max_inbox_bytes"] = 512
        self.bridge.save_config(cfg)

        # Queue a second short task on same pair -> must stay queued (same-pair isolation), if-shrink-1 stays in_flight
        self.bridge.submit_task(pair_id="loc_r3_if", message="Short 2nd", task_id="if-shrink-2")
        self.bridge.scan(recover=False)
        self.assertTrue(in_fl_file.exists())
        self.assertFalse((self.root / "failed/if-shrink-1.task.json").exists())
        self.assertEqual(self.bridge.get_task_status("if-shrink-1")["status"], "in_flight")
        self.assertEqual(self.bridge.get_task_status("if-shrink-2")["status"], "queued")

        # Simulate 2 hours elapsed -> transitions cleanly to timed_out (never hangs forever), then 2nd dispatches
        with self.bridge.ledger_tx() as ld:
            ld["dispatches"]["if-shrink-1"]["dispatchedMono"] = time.monotonic() - 7200.0
        self.bridge.scan(recover=False)
        self.assertEqual(self.bridge.get_task_status("if-shrink-1")["status"], "timed_out")
        self.assertEqual(self.bridge.get_task_status("if-shrink-2")["status"], "in_flight")

        # 2. Missing inbox/in_flight file for an active dispatch still preserves isolation, accepts outbox, and unblocks queued 2nd task
        cfg["max_inbox_bytes"] = 262144
        self.bridge.save_config(cfg)
        self.bridge.submit_task(pair_id="loc_r3_miss", message="Active missing file", task_id="if-miss-1")
        self.bridge.scan(recover=False)
        self.assertEqual(self.bridge.get_task_status("if-miss-1")["status"], "in_flight")
        (self.root / "inbox/in_flight/if-miss-1.task.json").unlink()

        self.bridge.submit_task(pair_id="loc_r3_miss", message="Waiting 2nd", task_id="if-miss-2")
        self.bridge.scan(recover=False)
        self.assertEqual(self.bridge.get_task_status("if-miss-1")["status"], "in_flight")
        self.assertEqual(self.bridge.get_task_status("if-miss-2")["status"], "queued")

        tb.atomic_json(self.root / "outbox/if-miss-1.json", {
            "taskId": "if-miss-1", "pairId": "loc_r3_miss", "message": "Reply despite missing in_flight file",
        })
        self.bridge.scan(recover=False)
        self.assertEqual(self.bridge.get_task_status("if-miss-1")["status"], "sent")
        self.assertEqual(self.bridge.get_task_status("if-miss-2")["status"], "in_flight")

    def test_review3_p1_nondict_json_and_mismatched_task_id_isolate_correctly(self) -> None:
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="loc_a", adapter="local", t3_thread=None,
                    gemini_title="Gem-R3A", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="loc_b", adapter="local", t3_thread=None,
                    gemini_title="Gem-R3B", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
        self.ax.add_conv("Gem-R3A")
        self.ax.add_conv("Gem-R3B")

        # 1. Valid non-dict JSON list at head of inbox MUST NOT crash scan or block subsequent valid tasks
        (self.root / "inbox/000-nondict.task.json").write_text('["not", "an", "object"]', encoding="utf-8")
        self.bridge.submit_task(pair_id="loc_a", message="Valid after nondict", task_id="001-after-nondict")
        self.bridge.scan(recover=False)
        self.assertTrue((self.root / "failed/000-nondict.task.json").exists())
        self.assertFalse((self.root / "inbox/000-nondict.task.json").exists())
        self.assertEqual(self.bridge.get_task_status("001-after-nondict")["status"], "in_flight")
        # Finish 001 so loc_a is free
        tb.atomic_json(self.root / "outbox/001-after-nondict.json", {"taskId": "001-after-nondict", "pairId": "loc_a", "message": "done"})
        self.bridge.scan(recover=False)

        # 2. Tampered file `task-a.task.json` claiming `taskId: "task-b"` must fail ONLY `task-a`, leaving `task-b` untouched & dispatched!
        self.bridge.submit_task(pair_id="loc_a", message="Legit A", task_id="task-a")
        self.bridge.submit_task(pair_id="loc_b", message="Legit B", task_id="task-b")
        loc_a_uid = self.bridge.pairs()["pairs"]["loc_a"]["pairUid"]
        tb.atomic_json(self.root / "inbox/task-a.task.json", {
            "schemaVersion": 2, "taskId": "task-b", "pairId": "loc_a", "pairUid": loc_a_uid,
            "replyThreadId": None, "replyMode": "auto", "message": "Spoof B from A",
        })
        self.bridge.scan(recover=False)
        self.assertTrue((self.root / "failed/task-a.task.json").exists())
        st_a = self.bridge.get_task_status("task-a")
        self.assertEqual(st_a["status"], "failed")
        self.assertEqual(st_a["reason"], "invalid_inbox_task")
        self.assertEqual(self.bridge.get_task_status("task-b")["status"], "in_flight")

    def test_active_pair_summary_uses_frozen_dispatch(self) -> None:
        tb.atomic_json(self.root / "inbox/in_flight/frozen.task.json", {
            "taskId": "frozen", "pairId": "unrelated",
        })
        with self.bridge.ledger_tx() as ld:
            ld["dispatches"]["frozen"] = {"pairId": "original", "status": "in_flight"}
        self.assertEqual(self.bridge._active_in_flight_by_pair(), {"original": "frozen"})
        with self.bridge.ledger_tx() as ld:
            ld["dispatches"]["frozen"]["status"] = "sent"
        self.assertEqual(self.bridge._active_in_flight_by_pair(), {})

    def test_review4_p1_in_flight_tampered_task_id_isolates_file_and_both_pairs_complete(self) -> None:
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="loc_a", adapter="local", t3_thread=None,
                    gemini_title="Gem-R4A", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="loc_b", adapter="local", t3_thread=None,
                    gemini_title="Gem-R4B", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
        self.ax.add_conv("Gem-R4A")
        self.ax.add_conv("Gem-R4B")

        self.bridge.submit_task(pair_id="loc_a", message="Work on A", task_id="task-a")
        self.bridge.submit_task(pair_id="loc_b", message="Work on B", task_id="task-b")
        self.bridge.scan(recover=False)
        self.assertEqual(self.bridge.get_task_status("task-a")["status"], "in_flight")
        self.assertEqual(self.bridge.get_task_status("task-b")["status"], "in_flight")
        self.assertEqual(len(self.ax.injected_prompts), 2)

        # Tamper in_flight/task-a.task.json so body claims taskId="task-b" while pairId remains "loc_a"
        loc_a_uid = self.bridge.pairs()["pairs"]["loc_a"]["pairUid"]
        tb.atomic_json(self.root / "inbox/in_flight/task-a.task.json", {
            "schemaVersion": 2, "taskId": "task-b", "pairId": "loc_a", "pairUid": loc_a_uid,
            "replyThreadId": None, "replyMode": "auto", "message": "Confused taskId",
        })

        self.bridge.scan(recover=False)
        self.assertTrue((self.root / "failed/task-a.task.json").exists())
        self.assertFalse((self.root / "inbox/in_flight/task-a.task.json").exists())
        # BOTH dispatches MUST remain in_flight with zero duplicate AX sends
        self.assertEqual(self.bridge.get_task_status("task-a")["status"], "in_flight")
        self.assertEqual(self.bridge.get_task_status("task-b")["status"], "in_flight")
        self.assertEqual(len(self.ax.injected_prompts), 2)

        # Both pairs STILL complete cleanly via their legitimate outbox receipts!
        tb.atomic_json(self.root / "outbox/task-a.json", {"taskId": "task-a", "pairId": "loc_a", "message": "Done A"})
        tb.atomic_json(self.root / "outbox/task-b.json", {"taskId": "task-b", "pairId": "loc_b", "message": "Done B"})
        self.bridge.scan(recover=False)
        self.assertEqual(self.bridge.get_task_status("task-a")["status"], "sent")
        self.assertEqual(self.bridge.get_task_status("task-b")["status"], "sent")
        self.assertEqual(self.bridge.read_task_payload("task-a")["message"], "Done A")
        self.assertEqual(self.bridge.read_task_payload("task-b")["message"], "Done B")

    def test_review4_p2_large_archived_reply_survives_lowering_message_and_receipt_limits(self) -> None:
        with mock.patch("sys.stdout", new=io.StringIO()):
            tb.cmd_pair(
                self.bridge,
                argparse.Namespace(
                    pair_cmd="add", id="loc_big", adapter="local", t3_thread=None,
                    gemini_title="Gem-R4Big", reply_to=None, label=None, workspace=None,
                    route="gemini", reply_mode="auto", force=False,
                ),
            )
        self.ax.add_conv("Gem-R4Big")

        cfg = self.bridge.config()
        cfg["max_message_bytes"] = 512 * 1024
        cfg["max_receipt_file_bytes"] = 768 * 1024
        self.bridge.save_config(cfg)

        self.bridge.submit_task(pair_id="loc_big", message="Short prompt", task_id="big-arch-1")
        self.bridge.scan(recover=False)
        big_reply = "B" * (300 * 1024)
        tb.atomic_json(self.root / "outbox/big-arch-1.json", {
            "taskId": "big-arch-1", "pairId": "loc_big", "message": big_reply,
        })
        self.bridge.scan(recover=False)
        rc1, out1 = self.bridge.wait_for_task("big-arch-1", timeout_s=1.0)
        self.assertEqual(rc1, 0)
        self.assertEqual(out1["message"], big_reply)

        # Lower both message and receipt limits back to defaults -> archived reply MUST remain readable!
        cfg["max_message_bytes"] = 32768
        cfg["max_receipt_file_bytes"] = 65536
        self.bridge.save_config(cfg)

        self.assertEqual(self.bridge.read_task_payload("big-arch-1")["message"], big_reply)
        rc2, out2 = self.bridge.wait_for_task("big-arch-1", timeout_s=1.0)
        self.assertEqual(rc2, 0)
        self.assertTrue(out2["ok"])
        self.assertEqual(out2["message"], big_reply)


if __name__ == "__main__":
    unittest.main()
