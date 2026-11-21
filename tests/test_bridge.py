#!/usr/bin/python3
"""Offline unit tests for T3 Code <-> Gemini Desktop 1-to-1 bridge."""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills/t3-gemini-bridge/scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import bridge_ax  # noqa: E402
import t3_gemini_bridge as tb  # noqa: E402


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
        iso_root = self.tmp / "iso_share"
        iso_bin = self.tmp / "iso_bin"

        res1 = subprocess.run(
            ["bash", str(install_sh), "--root", str(iso_root), "--bin-dir", str(iso_bin), "--label", "io.test.bridge"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res1.returncode, 0, msg=res1.stderr or res1.stdout)
        wrapper = iso_bin / "t3-gemini-bridge"
        self.assertTrue(wrapper.is_file() and os.access(wrapper, os.X_OK))

        status_res = subprocess.run([str(wrapper), "status", "--json"], capture_output=True, text=True, check=False)
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
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res2.returncode, 0, msg=res2.stderr or res2.stdout)
        after = json.loads(pairs_file.read_text(encoding="utf-8"))
        self.assertIn("keep_me", after["pairs"])

    def test_26_invalid_launchagent_label_rejected(self) -> None:
        for bad_label in ("../evil", "bad/label", "", ".hidden", "has space"):
            with self.assertRaises(tb.BridgeError) as cm:
                tb._validated_label(bad_label)
            self.assertEqual(str(cm.exception), "invalid_launchagent_label")

        lbl, plist_path = tb._validated_label("io.example.t3-gemini-bridge")
        self.assertEqual(lbl, "io.example.t3-gemini-bridge")
        self.assertEqual(plist_path.name, "io.example.t3-gemini-bridge.plist")


if __name__ == "__main__":
    unittest.main()
