#!/usr/bin/python3
"""T3 Code <-> Gemini Desktop App 1-to-1 automatic bidirectional bridge.

Python 3.9+ standard library only; no T3 database/token manipulation.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import getpass
import hashlib
import json
import logging
import logging.handlers
import math
import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from typing import Any
import uuid

SCRIPT = Path(__file__).resolve()
if str(SCRIPT.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT.parent))

import bridge_ax  # noqa: E402

LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
HOST_HOME = os.environ.get("HOME") or str(Path.home())
DEFAULT_ROOT = Path(
    os.environ.get("T3_GEMINI_BRIDGE_ROOT")
    or str(Path(HOST_HOME) / ".local/share/t3-gemini-bridge")
)
DEFAULT_T3_HOME = (
    os.environ.get("T3_GEMINI_BRIDGE_T3_HOME")
    or str(Path(HOST_HOME) / ".t3")
)
LABEL = os.environ.get("T3_GEMINI_BRIDGE_LABEL") or "io.t3-gemini-bridge.daemon"
PLIST = Path(HOST_HOME) / "Library/LaunchAgents" / (LABEL + ".plist")


def resolve_default_t3_cli(t3_home: str = DEFAULT_T3_HOME) -> str:
    """Resolve the `t3` CLI binary via env override, T3 home bin, or PATH."""
    env_cli = os.environ.get("T3_GEMINI_BRIDGE_CLI", "").strip()
    if env_cli:
        return env_cli
    local_cli = Path(t3_home) / "bin/t3"
    if local_cli.exists() and os.access(str(local_cli), os.X_OK):
        return str(local_cli)
    search_path = os.environ.get("PATH", "")
    extra = "/opt/homebrew/bin:/usr/local/bin"
    combined = f"{search_path}:{extra}" if search_path else extra
    found = shutil.which("t3", path=combined)
    if found:
        return found
    return str(local_cli)


DEFAULT_T3_CLI = resolve_default_t3_cli(DEFAULT_T3_HOME)

DIRS = (
    "inbox",
    "inbox/in_flight",
    "inbox/done",
    "outbox",
    "sending",
    "sent",
    "failed",
    "dedup",
    "logs",
)

TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
PAIR_ID_RE = re.compile(r"^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff_.-]{0,63}$")
THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,255}$")

SENTINEL_START_PREFIX = "[[T3_BRIDGE_START:"
SENTINEL_DONE_PREFIX = "[[T3_BRIDGE_DONE:"
SENTINEL_SUFFIX = "]]"

NOTIFY_SCRIPT = """on run argv
display notification (item 2 of argv) with title (item 1 of argv)
end run"""

_UI_NOISE_EXACT = frozenset({
    "Gemini said",
    "Gemini 说",
    "Show thinking",
    "显示思路",
    "Hide thinking",
    "隐藏思路",
    "Copy",
    "拷贝",
    "Copy response",
    "Export to Docs",
    "Send feedback",
    "Good response",
    "Bad response",
    "Reply",
    "Try again",
    "Completed",
    "已完成",
    "In progress",
    "进行中",
})


class BridgeError(Exception):
    """Fixed, non-sensitive error code."""


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def make_task_id() -> str:
    ts = datetime.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return f"{ts}-{uuid.uuid4().hex[:6]}"


def fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def no_symlink(path: Path | str) -> None:
    p = Path(path)
    for part in (p,) + tuple(p.parents):
        if part.is_symlink():
            raise BridgeError("symlink_path_rejected")


def read_bytes(path: Path, limit: int) -> bytes:
    no_symlink(path)
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BridgeError("non_regular_or_hardlinked_file")
        if info.st_size > limit:
            raise BridgeError("file_too_large")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        after = os.fstat(fd)
        if len(data) > limit:
            raise BridgeError("file_too_large")
        if (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise BridgeError("file_changed_during_read")
        return data
    finally:
        os.close(fd)


def decode_json(data: bytes | str) -> Any:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise BridgeError("duplicate_json_key")
            result[key] = value
        return result

    try:
        return json.loads(
            data,
            object_pairs_hook=unique_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(BridgeError("nonfinite_json")),
        )
    except (ValueError, UnicodeError, RecursionError):
        raise BridgeError("invalid_json") from None


def atomic_json(path: Path, value: Any) -> None:
    no_symlink(path)
    data = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    tmp = path.parent / ("._" + path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(tmp), str(path))
        fsync_dir(path.parent)
    finally:
        if tmp.exists():
            tmp.unlink()


@contextlib.contextmanager
def file_lock(path: Path, nonblocking: bool = False):
    no_symlink(path)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        yield fd
    finally:
        os.close(fd)


def _current_username(home_dir: str) -> str:
    for candidate in (os.environ.get("USER"), os.environ.get("LOGNAME")):
        if candidate and candidate.strip():
            return candidate.strip()
    try:
        user = getpass.getuser()
        if user and user.strip():
            return user.strip()
    except Exception:
        pass
    home_name = Path(home_dir).name
    return home_name if home_name else "user"


def child_env() -> dict[str, str]:
    home_dir = os.environ.get("HOME") or HOST_HOME
    user = _current_username(home_dir)
    result = {
        "HOME": home_dir,
        "USER": user,
        "LOGNAME": os.environ.get("LOGNAME") or user,
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "en_US.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for key in (
        "TMPDIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        if key in os.environ:
            result[key] = os.environ[key]
    return result


def default_config() -> dict[str, Any]:
    return {
        "schemaVersion": 2,
        "global_mode": "hybrid",
        "poll_interval_seconds": 2.0,
        "notifications": True,
        "ax_capture_grace_seconds": 6.0,
        "max_task_wait_seconds": 1800,
        "max_message_bytes": 32768,
        "max_message_lines": 400,
        "max_receipt_file_bytes": 65536,
        "max_inbox_bytes": 262144,
    }


def default_pairs() -> dict[str, Any]:
    return {"schemaVersion": 2, "pairs": {}}


def default_ledger() -> dict[str, Any]:
    return {"schemaVersion": 2, "tasks": {}, "dispatches": {}}


def build_framed_prompt(
    task_id: str,
    pair_id: str,
    reply_thread_id: str,
    message: str,
    outbox_path: Path,
) -> str:
    """Build a prompt wrapped with split-literal sentinels so prompt echoes never trigger completion."""
    start_tag = f"{SENTINEL_START_PREFIX}{task_id}{SENTINEL_SUFFIX}"
    # Deliberately split done prefix and task_id suffix in instructions (Prompt-Echo Immunity).
    return (
        f"{start_tag}\n"
        f"[T3->Gemini Auto-Dispatch] taskId={task_id} | pairId={pair_id} | replyThreadId={reply_thread_id}\n"
        f"When finished, report results via either method (no manual confirmation needed):\n"
        f"1) Structured receipt (recommended for long reports or file paths): atomically write `{outbox_path}` "
        f"with JSON containing only "
        f"`{{\"taskId\": \"{task_id}\", \"pairId\": \"{pair_id}\", \"message\": \"<full summary and absolute paths>\"}}`;\n"
        f"2) Or reply directly in this chat and end with a standalone line formed by concatenating "
        f"\"{SENTINEL_DONE_PREFIX}\" and \"{task_id}{SENTINEL_SUFFIX}\" with no spaces or quotes between them.\n"
        f"--- TASK ---\n"
        f"{message.rstrip()}\n"
    )


def _clip_to_limits(text: str, max_bytes: int, max_lines: int) -> str:
    lines = text.splitlines()
    notice = "\n\n[Note: Reply truncated to fit bridge size limits; inspect the paired Gemini Desktop conversation for the full response.]"
    notice_bytes = len(notice.encode("utf-8"))
    clipped = False

    if len(lines) > max_lines:
        lines = lines[: max(1, max_lines - 3)]
        clipped = True
    out = "\n".join(lines).strip()
    target_bytes = max_bytes - (notice_bytes if clipped else 0)
    encoded = out.encode("utf-8")
    if len(encoded) > target_bytes:
        clipped = True
        budget = max(256, max_bytes - notice_bytes)
        out = encoded[:budget].decode("utf-8", errors="ignore").rstrip()
    if clipped:
        out = out + notice
    return out


def extract_gemini_reply(
    poll_text: str,
    task_id: str,
    baseline_text: str = "",
    baseline_chunks: list[str] | None = None,
    current_chunks: list[str] | None = None,
    max_bytes: int = 31000,
    max_lines: int = 380,
) -> tuple[bool, str]:
    """Extract only Gemini's new reply for task_id, ignoring prior turns, prompt echoes, and UI chrome."""
    start_marker = f"{SENTINEL_START_PREFIX}{task_id}{SENTINEL_SUFFIX}"
    done_marker = f"{SENTINEL_DONE_PREFIX}{task_id}{SENTINEL_SUFFIX}"

    if current_chunks is not None:
        chunks = [c for c in current_chunks if c and c.strip()]
        base = [c for c in (baseline_chunks or []) if c and c.strip()]

        # 1. Start strictly after our unique start_marker if present
        last_start_idx = -1
        for idx, ch in enumerate(chunks):
            if start_marker in ch:
                last_start_idx = idx
        cut_idx = (last_start_idx + 1) if last_start_idx != -1 else 0

        # 2. Always enforce a floor past any OTHER turn's DONE sentinel (defense-in-depth)
        for idx, ch in enumerate(chunks):
            for m in re.finditer(r"\[\[T3_BRIDGE_DONE:[^\]]+\]\]", ch):
                if m.group(0) != done_marker:
                    cut_idx = max(cut_idx, idx + 1)

        # 3. If start_marker was missing or baseline tail had not yet appeared before last_start_idx
        #    (e.g. folded top bubbles or non-chronological UI chrome), advance past matching baseline suffix.
        baseline_already_passed = (
            bool(base)
            and last_start_idx != -1
            and base[-1] in chunks[:last_start_idx]
        )
        if base and not baseline_already_passed:
            if last_start_idx == -1:
                common = 0
                limit = min(len(base), len(chunks))
                while common < limit and chunks[common] == base[common]:
                    common += 1
                cut_idx = max(cut_idx, common)
            done_idx = next((i for i, c in enumerate(chunks) if done_marker in c), len(chunks))
            best_after = cut_idx
            for b_start in range(len(base)):
                tail = base[b_start:]
                tlen = len(tail)
                max_pos = min(done_idx, len(chunks)) - tlen
                for pos in range(cut_idx, max_pos + 1):
                    if chunks[pos : pos + tlen] == tail:
                        best_after = max(best_after, pos + tlen)
                        break
                if best_after > cut_idx:
                    break
            cut_idx = best_after

        candidate_chunks = chunks[cut_idx:]
        raw_reply = "\n".join(candidate_chunks)
    else:
        raw_reply = poll_text or ""
        idx = raw_reply.rfind(start_marker)
        if idx != -1:
            # Drop up through "--- TASK ---" prompt section if present
            after = raw_reply[idx:]
            task_sep = after.find("--- TASK ---")
            if task_sep != -1:
                after = after[task_sep + len("--- TASK ---") :]
            raw_reply = after
        elif baseline_text and raw_reply.startswith(baseline_text):
            raw_reply = raw_reply[len(baseline_text) :]

    has_sentinel = done_marker in raw_reply

    # Remove echoed prompt chunks or sentinel/chrome lines
    cleaned_lines: list[str] = []
    skip_prompt_echo = False
    for line in raw_reply.splitlines():
        s = line.strip()
        if start_marker in s:
            skip_prompt_echo = True
            continue
        if skip_prompt_echo:
            if "--- TASK ---" in s:
                skip_prompt_echo = False
            continue
        if s == done_marker:
            continue
        if done_marker in s:
            s = s.replace(done_marker, "").strip()
            if not s:
                continue
            line = s
        if s in _UI_NOISE_EXACT or s in bridge_ax.CHROME_TEXTS:
            continue
        cleaned_lines.append(line)

    cleaned = "\n".join(cleaned_lines).strip()
    if not cleaned:
        return has_sentinel, ""
    return has_sentinel, _clip_to_limits(cleaned, max_bytes=max_bytes, max_lines=max_lines)


def validate_pairs_data(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("schemaVersion") != 2 or not isinstance(data.get("pairs"), dict):
        raise BridgeError("invalid_pairs_schema")
    seen_t3: dict[str, str] = {}
    seen_gem: dict[str, str] = {}
    for pid, entry in data["pairs"].items():
        if not isinstance(pid, str) or not PAIR_ID_RE.fullmatch(pid):
            raise BridgeError("invalid_pair_id")
        if not isinstance(entry, dict):
            raise BridgeError("invalid_pair_entry")
        if entry.get("pairId") != pid:
            raise BridgeError("pair_id_mismatch")
        t3_id = entry.get("t3ThreadId")
        rep_id = entry.get("defaultReplyThreadId")
        gem_title = entry.get("geminiTitle")
        route = entry.get("route")
        reply_mode = entry.get("replyMode", "auto")
        enabled = entry.get("enabled", True)
        if not isinstance(t3_id, str) or not THREAD_ID_RE.fullmatch(t3_id):
            raise BridgeError("invalid_pair_t3_thread_id")
        if not isinstance(rep_id, str) or not THREAD_ID_RE.fullmatch(rep_id):
            raise BridgeError("invalid_pair_reply_thread_id")
        if not isinstance(gem_title, str) or not gem_title.strip():
            raise BridgeError("invalid_pair_gemini_title")
        if route not in ("gemini", "t3"):
            raise BridgeError("invalid_pair_route")
        if reply_mode not in ("auto", "outbox", "ax_capture"):
            raise BridgeError("invalid_pair_reply_mode")
        if not isinstance(enabled, bool):
            raise BridgeError("invalid_pair_enabled")
        allowed_replies = entry.get("allowedReplyThreadIds")
        if allowed_replies is not None:
            if not isinstance(allowed_replies, list) or any(
                not isinstance(x, str) or not THREAD_ID_RE.fullmatch(x) for x in allowed_replies
            ):
                raise BridgeError("invalid_allowed_reply_threads")
        if enabled:
            if t3_id in seen_t3:
                raise BridgeError("duplicate_t3_thread_in_pairs")
            if gem_title.strip() in seen_gem:
                raise BridgeError("duplicate_gemini_title_in_pairs")
            seen_t3[t3_id] = pid
            seen_gem[gem_title.strip()] = pid
    return data


def resolve_effective_route(config: dict[str, Any], pair: dict[str, Any]) -> str:
    if config.get("global_mode") == "t3_only":
        return "t3"
    if pair.get("route") == "t3":
        return "t3"
    return "gemini"


def _allowed_reply_targets_for_pair(pair: dict[str, Any], pairs_data: dict[str, Any]) -> set[str]:
    targets = {pair["t3ThreadId"], pair["defaultReplyThreadId"]}
    for item in pair.get("allowedReplyThreadIds") or []:
        targets.add(item)
    for other in pairs_data.get("pairs", {}).values():
        if isinstance(other, dict) and other.get("enabled", True) and isinstance(other.get("t3ThreadId"), str):
            targets.add(other["t3ThreadId"])
    return targets


class Bridge:
    def __init__(
        self,
        root: Path | str = DEFAULT_ROOT,
        cli_path: str | None = None,
        t3_home: str | None = None,
        ax_driver: Any = None,
    ) -> None:
        self.root = Path(root)
        self.t3_home = t3_home if t3_home is not None else (os.environ.get("T3_GEMINI_BRIDGE_T3_HOME") or DEFAULT_T3_HOME)
        self.cli_path = cli_path if cli_path is not None else resolve_default_t3_cli(self.t3_home)
        self._ax_driver = ax_driver
        self.logger: logging.Logger | None = None
        self.stop = False
        self.last_error: str | None = None
        self.started_at = now_iso()
        self._ax_missing_notified = False
        self._locked_notified = False
        self._dispatch_cooldown: dict[str, tuple[float, int, str]] = {}

    def ax(self) -> Any:
        if self._ax_driver is not None:
            return self._ax_driver
        drv = bridge_ax.MacOSAXBridge.get_instance()
        if drv is not None:
            self._ax_driver = drv
        return drv

    def system_probes(self) -> dict[str, Any]:
        drv = self.ax()
        if drv is not None and hasattr(drv, "system_probes"):
            return dict(drv.system_probes())
        pid = bridge_ax.find_gemini_pid()
        return {
            "axTrusted": bridge_ax.is_ax_trusted(),
            "screenLocked": bridge_ax.is_screen_locked(),
            "geminiRunning": pid is not None,
            "geminiPid": pid,
        }

    def setup(self) -> None:
        no_symlink(self.root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name in DIRS:
            p = self.root / name
            no_symlink(p)
            p.mkdir(mode=0o700, parents=True, exist_ok=True)

        no_symlink(self.root / "logs/bridge.log")
        logger = logging.getLogger("t3_gemini_bridge_v2." + str(self.root))
        if not logger.handlers:
            handler = logging.handlers.RotatingFileHandler(
                str(self.root / "logs/bridge.log"),
                maxBytes=1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            logger.propagate = False
        self.logger = logger

        with file_lock(self.root / ".config.lock"):
            if not (self.root / "bridge_config.json").exists():
                atomic_json(self.root / "bridge_config.json", default_config())

        with file_lock(self.root / ".pairs.lock"):
            if not (self.root / "pairs.json").exists():
                atomic_json(self.root / "pairs.json", default_pairs())

        with file_lock(self.root / ".ledger.lock"):
            if not (self.root / "ledger.json").exists():
                if any(any((self.root / name).iterdir()) for name in ("sending", "sent", "failed", "dedup")):
                    raise BridgeError("ledger_missing_existing_history")
                atomic_json(self.root / "ledger.json", default_ledger())

    def log(self, event: str, task_id: str | None = None, reason: str | None = None, **extra: Any) -> None:
        if self.logger:
            payload: dict[str, Any] = {"event": event}
            if task_id and TASK_ID_RE.fullmatch(task_id):
                payload["taskId"] = task_id
            if reason:
                payload["reason"] = reason
            for k, v in extra.items():
                if isinstance(v, (str, int, float, bool)) or v is None:
                    payload[k] = v
            self.logger.info(json.dumps(payload, ensure_ascii=False))

    def config(self) -> dict[str, Any]:
        c = decode_json(read_bytes(self.root / "bridge_config.json", 65536))
        if not isinstance(c, dict) or c.get("schemaVersion") != 2:
            raise BridgeError("invalid_config")
        if c.get("global_mode") not in ("hybrid", "t3_only"):
            raise BridgeError("invalid_global_mode")
        if not isinstance(c.get("notifications"), bool):
            raise BridgeError("invalid_notifications_flag")
        poll = c.get("poll_interval_seconds", 2.0)
        if isinstance(poll, bool) or not isinstance(poll, (int, float)) or not math.isfinite(poll) or not 0.2 <= poll <= 300:
            raise BridgeError("invalid_poll_interval")
        c["poll_interval_seconds"] = float(poll)
        grace = c.get("ax_capture_grace_seconds", 6.0)
        if isinstance(grace, bool) or not isinstance(grace, (int, float)) or not math.isfinite(grace) or not 0.2 <= grace <= 120:
            raise BridgeError("invalid_ax_capture_grace")
        c["ax_capture_grace_seconds"] = float(grace)
        max_wait = c.get("max_task_wait_seconds", 1800)
        if isinstance(max_wait, bool) or not isinstance(max_wait, int) or not 10 <= max_wait <= 86400:
            raise BridgeError("invalid_max_task_wait")
        for key, default, ceiling in (
            ("max_message_bytes", 32768, 1048576),
            ("max_message_lines", 400, 10000),
            ("max_receipt_file_bytes", 65536, 2097152),
            ("max_inbox_bytes", 262144, 2097152),
        ):
            val = c.get(key, default)
            if isinstance(val, bool) or not isinstance(val, int) or not 1 <= val <= ceiling:
                raise BridgeError("invalid_size_limit")
            c[key] = val
        return c

    def save_config(self, cfg: dict[str, Any]) -> None:
        with file_lock(self.root / ".config.lock"):
            atomic_json(self.root / "bridge_config.json", cfg)

    def pairs(self) -> dict[str, Any]:
        raw = decode_json(read_bytes(self.root / "pairs.json", 524288))
        return validate_pairs_data(raw)

    @contextlib.contextmanager
    def pairs_tx(self):
        with file_lock(self.root / ".pairs.lock"):
            data = self.pairs()
            yield data
            validate_pairs_data(data)
            atomic_json(self.root / "pairs.json", data)

    def ledger(self) -> dict[str, Any]:
        val = decode_json(read_bytes(self.root / "ledger.json", 32 * 1024 * 1024))
        if (
            not isinstance(val, dict)
            or val.get("schemaVersion") != 2
            or not isinstance(val.get("tasks"), dict)
            or not isinstance(val.get("dispatches"), dict)
        ):
            raise BridgeError("invalid_ledger")
        for key, item in val["tasks"].items():
            if (
                not TASK_ID_RE.fullmatch(key)
                or not isinstance(item, dict)
                or item.get("status") not in ("sending", "sent", "unknown_timeout", "failed")
            ):
                raise BridgeError("invalid_ledger_task")
        return val

    @contextlib.contextmanager
    def ledger_tx(self):
        with file_lock(self.root / ".ledger.lock"):
            data = self.ledger()
            yield data
            atomic_json(self.root / "ledger.json", data)

    def move(self, path: Path, directory: str) -> Path:
        parent = self.root / directory
        no_symlink(parent)
        dst = parent / path.name
        if dst.exists() or dst.is_symlink():
            dst = parent / (path.stem + "." + uuid.uuid4().hex[:12] + path.suffix)
        os.replace(str(path), str(dst))
        fsync_dir(parent)
        fsync_dir(path.parent)
        return dst

    def notify(self, title: str, body: str, enabled: bool) -> str:
        if not enabled:
            return "disabled"
        try:
            p = subprocess.run(
                ["/usr/bin/osascript", "-e", NOTIFY_SCRIPT, title, body],
                capture_output=True,
                timeout=10,
                env=child_env(),
            )
            return "submitted" if p.returncode == 0 else "command_failed"
        except (OSError, subprocess.TimeoutExpired):
            return "command_failed"

    def write_health(self, probes: dict[str, Any], cfg: dict[str, Any] | None) -> None:
        health = {
            "schemaVersion": 1,
            "pid": os.getpid(),
            "startedAt": self.started_at,
            "lastScanAt": now_iso(),
            "axTrusted": bool(probes.get("axTrusted", False)),
            "screenLocked": bool(probes.get("screenLocked", False)),
            "geminiRunning": bool(probes.get("geminiRunning", False)),
            "geminiPid": probes.get("geminiPid"),
            "globalMode": cfg.get("global_mode") if cfg else None,
            "lastError": self.last_error,
        }
        try:
            atomic_json(self.root / ".daemon_health.json", health)
        except Exception:
            pass

    def read_health(self) -> dict[str, Any] | None:
        hp = self.root / ".daemon_health.json"
        if not hp.exists():
            return None
        try:
            return decode_json(read_bytes(hp, 65536))
        except Exception:
            return None

    def deliver_to_t3_cli(self, thread_id: str, message: str) -> dict[str, Any]:
        if not THREAD_ID_RE.fullmatch(thread_id):
            raise BridgeError("invalid_target_thread_id")
        if not message or not message.strip() or "\x00" in message:
            raise BridgeError("invalid_delivery_message")
        started = time.monotonic()
        try:
            result = subprocess.run(
                [self.cli_path, "thread", "send", "--base-dir", self.t3_home, "--json", thread_id, "-"],
                input=message,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=60,
                check=False,
                env=child_env(),
                cwd=str(self.root),
            )
        except subprocess.TimeoutExpired:
            raise BridgeError("cli_timeout_no_retry") from None
        except OSError:
            raise BridgeError("cli_spawn_failed") from None
        duration_ms = round((time.monotonic() - started) * 1000.0, 1)
        if result.returncode != 0:
            raise BridgeError(f"cli_nonzero_exit_{result.returncode}")
        if len(result.stdout.encode("utf-8")) > 1048576:
            raise BridgeError("cli_output_too_large")
        reply = decode_json(result.stdout)
        if not isinstance(reply, dict) or reply.get("isError") is True:
            raise BridgeError("invalid_cli_receipt")
        if "threadId" in reply and reply["threadId"] != thread_id:
            raise BridgeError("receipt_target_mismatch")
        summary: dict[str, Any] = {}
        for k in ("threadId", "runId", "taskId"):
            v = reply.get(k)
            if isinstance(v, str) and THREAD_ID_RE.fullmatch(v):
                summary[k] = v
        for k in ("delivery", "status"):
            v = reply.get(k)
            if isinstance(v, str):
                summary[k] = v
        return {"durationMs": duration_ms, "t3": summary}

    def record_task_failure(
        self,
        task_id: str,
        status: str,
        reason: str,
        cfg: dict[str, Any],
        exit_code: int | None = None,
    ) -> None:
        notice = self.notify(
            "T3-Gemini Bridge Delivery Error",
            f"Task {task_id} receipt delivery failed or timed out ({reason}); auto-retry disabled.",
            cfg["notifications"],
        )
        with self.ledger_tx() as ledger:
            rec = ledger["tasks"].setdefault(task_id, {})
            rec.update(status=status, updatedAt=now_iso(), reason=reason, notification=notice)
            if exit_code is not None:
                rec["exitCode"] = exit_code
            if task_id in ledger["dispatches"] and status in ("failed", "unknown_timeout"):
                ledger["dispatches"][task_id]["status"] = status
                ledger["dispatches"][task_id]["updatedAt"] = now_iso()
                ledger["dispatches"][task_id]["reason"] = reason
        self.log(status, task_id, reason)

    def validate_outbox_receipt(
        self,
        data: Any,
        filename: str,
        cfg: dict[str, Any],
        pairs_data: dict[str, Any],
        ledger_data: dict[str, Any],
    ) -> tuple[str, str, str]:
        """Return (resolved_thread_id, role_label, pair_id_or_target)."""
        if not isinstance(data, dict):
            raise BridgeError("invalid_fields")
        task_id = data.get("taskId")
        message = data.get("message")
        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id) or filename != f"{task_id}.json":
            raise BridgeError("invalid_task_id_or_filename")
        if not isinstance(message, str) or not message.strip():
            raise BridgeError("empty_or_nonstring_message")
        if (
            "\x00" in message
            or len(message.encode("utf-8")) > cfg["max_message_bytes"]
            or len(message.splitlines()) > cfg["max_message_lines"]
        ):
            raise BridgeError("message_limit_exceeded")

        keys = set(data.keys())
        # v2 schema: {"taskId", "pairId", "message", optional "captureChannel", optional "role"}
        if "pairId" in data:
            allowed_keys = {"taskId", "pairId", "message", "captureChannel", "role"}
            if not keys.issubset(allowed_keys):
                raise BridgeError("invalid_fields")
            pair_id = data.get("pairId")
            if not isinstance(pair_id, str) or pair_id not in pairs_data["pairs"]:
                raise BridgeError("unknown_pair_id")
            pair = pairs_data["pairs"][pair_id]
            dispatch = ledger_data["dispatches"].get(task_id)
            if dispatch and isinstance(dispatch.get("replyThreadId"), str):
                target_tid = dispatch["replyThreadId"]
            else:
                target_tid = pair["defaultReplyThreadId"]
            if target_tid not in _allowed_reply_targets_for_pair(pair, pairs_data):
                raise BridgeError("reply_target_not_whitelisted")
            role_label = str(data.get("role") or pair.get("label") or pair_id)[:80]
            return target_tid, role_label, pair_id

        # v1 compatibility schema: {"taskId", "role", "target", "message"}
        if keys == {"taskId", "role", "target", "message"}:
            role = data.get("role")
            target = data.get("target")
            if not isinstance(role, str) or not role.strip() or len(role) > 80 or any(ord(c) < 32 for c in role):
                raise BridgeError("invalid_role")
            if not isinstance(target, str) or not target.strip():
                raise BridgeError("invalid_target")
            if target in pairs_data["pairs"]:
                pair = pairs_data["pairs"][target]
                return pair["defaultReplyThreadId"], role, target
            legacy_targets = cfg.get("legacy_targets")
            if isinstance(legacy_targets, dict) and target in legacy_targets:
                tid = legacy_targets[target]
                if isinstance(tid, str) and THREAD_ID_RE.fullmatch(tid):
                    return tid, role, target
            raise BridgeError("target_not_whitelisted")

        raise BridgeError("invalid_fields")

    def quarantine_invalid_outbox(self, path: Path, cfg: dict[str, Any], reason: str, task_id: str | None = None) -> None:
        if path.is_symlink():
            self.log("input_rejected", reason="symlink_input_rejected")
            return
        self.move(path, "failed")
        if task_id and TASK_ID_RE.fullmatch(task_id):
            with self.ledger_tx() as ledger:
                if task_id not in ledger["tasks"]:
                    ledger["tasks"][task_id] = {"status": "failed", "updatedAt": now_iso(), "reason": reason}
        self.notify("Gemini Bridge Receipt Error", f"Invalid receipt not sent ({reason}); moved to failed/.", cfg["notifications"])
        self.log("input_rejected", task_id, reason)

    def recover(self, cfg: dict[str, Any]) -> None:
        with file_lock(self.root / ".ledger.lock"):
            pending = {k for k, v in self.ledger()["tasks"].items() if v["status"] == "sending"}
        for path in sorted((self.root / "sending").glob("*.json")):
            if path.is_symlink() or path.name.startswith((".", "_")):
                continue
            tid = path.stem
            if not TASK_ID_RE.fullmatch(tid):
                self.move(path, "failed")
                continue
            with file_lock(self.root / ".ledger.lock"):
                prev = self.ledger()["tasks"].get(tid, {})
            if prev.get("status") == "sent":
                self.move(path, "sent")
            else:
                self.move(path, "failed")
                pending.add(tid)
        for tid in sorted(pending):
            self.record_task_failure(tid, "unknown_timeout", "recovered_interrupted_send_no_retry", cfg)

    def _archive_in_flight(self, task_id: str, dest_dir: str = "inbox/done") -> None:
        src = self.root / "inbox/in_flight" / f"{task_id}.task.json"
        if src.exists() and not src.is_symlink():
            self.move(src, dest_dir)

    def process_one_outbox(self, path: Path, cfg: dict[str, Any]) -> None:
        if path.name.startswith((".", "_")) or path.suffix != ".json" or path.is_symlink():
            return
        task_id = path.stem if TASK_ID_RE.fullmatch(path.stem) else None
        try:
            raw = read_bytes(path, cfg["max_receipt_file_bytes"])
            data = decode_json(raw)
            candidate = data.get("taskId") if isinstance(data, dict) else None
            if isinstance(candidate, str) and TASK_ID_RE.fullmatch(candidate):
                with file_lock(self.root / ".ledger.lock"):
                    previous = self.ledger()["tasks"].get(candidate)
                if previous:
                    self.move(path, "dedup")
                    self._archive_in_flight(candidate, "inbox/done")
                    self.log("duplicate_suppressed", candidate, previous["status"])
                    return
            pairs_data = self.pairs()
            with file_lock(self.root / ".ledger.lock"):
                ledger_data = self.ledger()
            resolved_tid, role_label, target_tag = self.validate_outbox_receipt(
                data, path.name, cfg, pairs_data, ledger_data
            )
            task_id = data["taskId"]
        except (BridgeError, OSError) as exc:
            with file_lock(self.root / ".ledger.lock"):
                self.ledger()
            self.quarantine_invalid_outbox(
                path, cfg, str(exc) if isinstance(exc, BridgeError) else "input_io_error", task_id
            )
            return

        claimed = self.move(path, "sending")
        digest = hashlib.sha256(data["message"].encode("utf-8")).hexdigest()
        with self.ledger_tx() as ledger:
            ledger["tasks"][task_id] = {
                "status": "sending",
                "updatedAt": now_iso(),
                "startedAt": now_iso(),
                "target": target_tag,
                "resolvedThreadId": resolved_tid,
                "messageSha256": digest,
                "attempts": 0,
            }
        self.log("sending", task_id)

        try:
            latest_cfg = self.config()
            latest_pairs = self.pairs()
            if read_bytes(claimed, latest_cfg["max_receipt_file_bytes"]) != raw:
                raise BridgeError("input_changed_before_claim")
            with file_lock(self.root / ".ledger.lock"):
                latest_ledger = self.ledger()
            re_tid, _, _ = self.validate_outbox_receipt(data, claimed.name, latest_cfg, latest_pairs, latest_ledger)
            if re_tid != resolved_tid:
                raise BridgeError("target_changed_before_send")
        except (BridgeError, OSError) as exc:
            self.move(claimed, "failed")
            self.record_task_failure(
                task_id, "failed", str(exc) if isinstance(exc, BridgeError) else "preflight_io_error", cfg
            )
            return

        with self.ledger_tx() as ledger:
            ledger["tasks"][task_id]["attempts"] = 1
            ledger["tasks"][task_id]["cliInvokedAt"] = now_iso()

        try:
            res = self.deliver_to_t3_cli(resolved_tid, data["message"])
        except BridgeError as exc:
            reason = str(exc)
            status = "unknown_timeout" if reason == "cli_timeout_no_retry" else "failed"
            self.record_task_failure(task_id, status, reason, cfg)
            self.move(claimed, "failed")
            self._archive_in_flight(task_id, "failed")
            return

        receipt = {
            "schemaVersion": 2,
            "taskId": task_id,
            "role": role_label,
            "target": target_tag,
            "resolvedThreadId": resolved_tid,
            "captureChannel": data.get("captureChannel", "outbox_json"),
            "accepted": True,
            "acceptedAt": now_iso(),
            "messageSha256": digest,
            "cliExitCode": 0,
            "cliDurationMs": res["durationMs"],
            "t3": res["t3"],
        }
        atomic_json(self.root / "sent" / f"{task_id}.receipt.json", receipt)
        with self.ledger_tx() as ledger:
            ledger["tasks"][task_id].update(status="sent", updatedAt=now_iso(), receipt=receipt)
            if task_id in ledger["dispatches"]:
                ledger["dispatches"][task_id].update(status="completed", completedAt=now_iso(), updatedAt=now_iso())
        self.move(claimed, "sent")
        self._archive_in_flight(task_id, "inbox/done")
        self.log("sent", task_id)

    def process_outbox(self, cfg: dict[str, Any]) -> None:
        for path in sorted((self.root / "outbox").glob("*.json")):
            if self.stop:
                break
            self.process_one_outbox(path, cfg)

    def _active_in_flight_by_pair(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for path in sorted((self.root / "inbox/in_flight").glob("*.task.json")):
            if path.name.startswith((".", "_")) or path.is_symlink():
                continue
            try:
                env = decode_json(read_bytes(path, 524288))
                if isinstance(env, dict) and isinstance(env.get("pairId"), str) and isinstance(env.get("taskId"), str):
                    result[env["pairId"]] = env["taskId"]
            except Exception:
                continue
        return result

    def process_in_flight(self, cfg: dict[str, Any], probes: dict[str, Any]) -> None:
        in_flight_files = sorted((self.root / "inbox/in_flight").glob("*.task.json"))
        if not in_flight_files:
            return
        pairs_data = self.pairs()
        now_mono = time.monotonic()

        # First clean up any task already marked sent in ledger
        with file_lock(self.root / ".ledger.lock"):
            ledger_snap = self.ledger()

        remaining: list[tuple[Path, dict[str, Any]]] = []
        for path in in_flight_files:
            if path.name.startswith((".", "_")) or path.is_symlink():
                continue
            try:
                task = decode_json(read_bytes(path, cfg["max_inbox_bytes"]))
            except Exception:
                self.move(path, "failed")
                continue
            tid = task.get("taskId")
            if not isinstance(tid, str) or not TASK_ID_RE.fullmatch(tid):
                self.move(path, "failed")
                continue
            t_rec = ledger_snap["tasks"].get(tid)
            if t_rec and t_rec.get("status") == "sent":
                self.move(path, "inbox/done")
                with self.ledger_tx() as ld:
                    if tid in ld["dispatches"]:
                        ld["dispatches"][tid].update(status="completed", updatedAt=now_iso())
                continue
            if (self.root / "outbox" / f"{tid}.json").exists():
                continue
            remaining.append((path, task))

        if not remaining:
            return

        # Timeout check doesn't require GUI access
        still_active: list[tuple[Path, dict[str, Any]]] = []
        for path, task in remaining:
            tid = task["taskId"]
            d_rec = ledger_snap["dispatches"].get(tid, {})
            disp_mono = d_rec.get("dispatchedMono")
            # Also handle daemon restarts where dispatchedMono wasn't from current boot
            if not isinstance(disp_mono, (int, float)) or disp_mono > now_mono:
                with self.ledger_tx() as ld:
                    ld["dispatches"].setdefault(tid, {})["dispatchedMono"] = now_mono
                disp_mono = now_mono
            if (now_mono - disp_mono) > cfg["max_task_wait_seconds"]:
                self.move(path, "failed")
                self.notify("Gemini Task Timeout", f"Task {tid} exceeded {cfg['max_task_wait_seconds']}s without completion.", cfg["notifications"])
                with self.ledger_tx() as ld:
                    ld["dispatches"].setdefault(tid, {}).update(
                        status="timed_out", updatedAt=now_iso(), reason="max_task_wait_exceeded"
                    )
                self.log("in_flight_timed_out", tid, "max_task_wait_exceeded")
                continue
            still_active.append((path, task))

        if not still_active:
            return

        if not probes.get("axTrusted") or not probes.get("geminiRunning") or probes.get("screenLocked"):
            return
        drv = self.ax()
        pid = probes.get("geminiPid")
        if drv is None or not pid:
            return

        try:
            conv_info = drv.list_conversations(pid)
        except Exception:
            return
        badge_by_title = {
            c["title"]: c.get("badge")
            for c in conv_info.get("conversations", [])
            if isinstance(c, dict) and isinstance(c.get("title"), str)
        }

        for path, task in still_active:
            if self.stop:
                break
            tid = task["taskId"]
            pid_name = task.get("pairId", "")
            pair = pairs_data["pairs"].get(pid_name)
            if not pair:
                self.move(path, "failed")
                continue
            gem_title = pair["geminiTitle"]
            reply_mode = task.get("replyMode") or pair.get("replyMode", "auto")
            badge = badge_by_title.get(gem_title)

            with file_lock(self.root / ".ledger.lock"):
                d_rec = self.ledger()["dispatches"].get(tid, {})

            if badge == "needs_attention":
                if not d_rec.get("notifiedAttention"):
                    self.notify("Gemini Task Needs Attention", f"Conversation '{gem_title}' is waiting for manual confirmation in Gemini.", cfg["notifications"])
                    with self.ledger_tx() as ld:
                        ld["dispatches"].setdefault(tid, {}).update(
                            status="needs_attention", notifiedAttention=True, updatedAt=now_iso()
                        )
                continue

            if badge == "in_progress":
                if d_rec.get("idleSinceMono") is not None or d_rec.get("stableCount"):
                    with self.ledger_tx() as ld:
                        ld["dispatches"].setdefault(tid, {}).update(
                            status="in_flight", idleSinceMono=None, stableSinceMono=None, stableCount=0, updatedAt=now_iso()
                        )
                continue

            if reply_mode == "outbox":
                continue

            disp_mono = float(d_rec.get("dispatchedMono") or now_mono)
            if (now_mono - disp_mono) < 4.0:
                continue

            idle_since = d_rec.get("idleSinceMono")
            if idle_since is None or idle_since > now_mono:
                with self.ledger_tx() as ld:
                    ld["dispatches"].setdefault(tid, {}).update(idleSinceMono=now_mono, updatedAt=now_iso())
                continue

            grace = 1.5 if reply_mode == "ax_capture" else cfg["ax_capture_grace_seconds"]
            if (now_mono - float(idle_since)) < grace:
                continue

            # If outbox receipt appeared during grace window, let process_outbox handle it
            if (self.root / "outbox" / f"{tid}.json").exists():
                continue

            with file_lock(self.root / ".gemini_ui.lock"):
                ok_open, open_reason = drv.open_conversation(pid, gem_title)
                if not ok_open:
                    self.log("ax_capture_open_failed", tid, open_reason, pairId=pid_name)
                    continue
                poll = drv.poll_conversation(pid)

            if poll.get("window_title") != gem_title:
                self.log("ax_capture_title_mismatch", tid, "title_verification_failed")
                continue
            if poll.get("confirmation_card"):
                if not d_rec.get("notifiedAttention"):
                    self.notify("Gemini Task Needs Attention", f"Conversation '{gem_title}' is showing a confirmation card; please confirm in Gemini.", cfg["notifications"])
                    with self.ledger_tx() as ld:
                        ld["dispatches"].setdefault(tid, {}).update(
                            status="needs_attention", notifiedAttention=True, idleSinceMono=None, updatedAt=now_iso()
                        )
                continue
            if poll.get("busy"):
                with self.ledger_tx() as ld:
                    ld["dispatches"].setdefault(tid, {}).update(
                        status="in_flight", idleSinceMono=None, stableSinceMono=None, stableCount=0, updatedAt=now_iso()
                    )
                continue

            has_sentinel, reply_text = extract_gemini_reply(
                poll_text=str(poll.get("text") or ""),
                task_id=tid,
                baseline_text="",
                baseline_chunks=d_rec.get("baselineChunks"),
                current_chunks=poll.get("chunks"),
                max_bytes=max(1024, cfg["max_message_bytes"] - 512),
                max_lines=max(20, cfg["max_message_lines"] - 15),
            )
            if not reply_text:
                continue
            base_len = int(d_rec.get("baselineTextLen") or 0)
            cur_total_len = len(str(poll.get("text") or ""))
            if not has_sentinel and cur_total_len <= base_len + 8:
                continue

            prev_len = d_rec.get("lastPollLen")
            stable_since = d_rec.get("stableSinceMono")
            stable_count = int(d_rec.get("stableCount") or 0)
            cur_len = len(reply_text)
            if prev_len == cur_len and stable_since is not None and stable_since <= now_mono:
                stable_count += 1
                elapsed_stable = now_mono - float(stable_since)
                if stable_count >= 2 or (has_sentinel and elapsed_stable >= 0.5):
                    label = pair.get("label") or pid_name
                    msg = f"[Bridge Reply: {label} -> T3] Task: {tid}\n{reply_text}"
                    msg = _clip_to_limits(msg, cfg["max_message_bytes"], cfg["max_message_lines"])
                    receipt_payload = {
                        "taskId": tid,
                        "pairId": pid_name,
                        "captureChannel": "ax_capture_sentinel" if has_sentinel else "ax_capture_stable",
                        "message": msg,
                    }
                    atomic_json(self.root / "outbox" / f"{tid}.json", receipt_payload)
                    with self.ledger_tx() as ld:
                        ld["dispatches"].setdefault(tid, {}).update(
                            status="captured_to_outbox", updatedAt=now_iso()
                        )
                    self.log("ax_captured", tid, receipt_payload["captureChannel"], pairId=pid_name)
                    continue
            else:
                with self.ledger_tx() as ld:
                    ld["dispatches"].setdefault(tid, {}).update(
                        lastPollLen=cur_len,
                        stableSinceMono=now_mono,
                        stableCount=1,
                        updatedAt=now_iso(),
                    )

    def process_inbox(self, cfg: dict[str, Any], probes: dict[str, Any]) -> None:
        if cfg["global_mode"] != "hybrid":
            return
        inbox_files = [
            p for p in sorted((self.root / "inbox").glob("*.task.json"))
            if not p.name.startswith((".", "_")) and not p.is_symlink() and p.is_file()
        ]
        if not inbox_files:
            return

        if not probes.get("axTrusted"):
            if not self._ax_missing_notified:
                self._ax_missing_notified = True
                self.notify(
                    "T3-Gemini Bridge Needs Accessibility",
                    "Enable Python.app in System Settings -> Privacy & Security -> Accessibility to resume dispatch.",
                    cfg["notifications"],
                )
                self.log("dispatch_blocked", reason="ax_not_trusted")
            return
        self._ax_missing_notified = False

        if probes.get("screenLocked"):
            if not self._locked_notified:
                self._locked_notified = True
                self.log("dispatch_paused", reason="screen_locked")
            return
        self._locked_notified = False

        pid = probes.get("geminiPid")
        drv = self.ax()
        if not probes.get("geminiRunning") or not pid or drv is None:
            self.log("dispatch_waiting", reason="gemini_not_running")
            return

        pairs_data = self.pairs()
        active_by_pair = self._active_in_flight_by_pair()
        try:
            conv_info = drv.list_conversations(pid)
        except Exception:
            conv_info = {"conversations": []}
        badge_by_title = {
            c["title"]: c.get("badge")
            for c in conv_info.get("conversations", [])
            if isinstance(c, dict) and isinstance(c.get("title"), str)
        }
        now_mono = time.monotonic()

        for path in inbox_files:
            if self.stop:
                break
            try:
                task = decode_json(read_bytes(path, cfg["max_inbox_bytes"]))
                tid = task.get("taskId")
                pid_name = task.get("pairId")
                msg = task.get("message")
                reply_tid = task.get("replyThreadId")
                if (
                    not isinstance(tid, str)
                    or not TASK_ID_RE.fullmatch(tid)
                    or path.name != f"{tid}.task.json"
                    or not isinstance(pid_name, str)
                    or not isinstance(msg, str)
                    or not msg.strip()
                    or not isinstance(reply_tid, str)
                    or not THREAD_ID_RE.fullmatch(reply_tid)
                ):
                    raise BridgeError("invalid_inbox_task")
            except (BridgeError, OSError) as exc:
                self.move(path, "failed")
                self.log("inbox_task_rejected", reason=str(exc) if isinstance(exc, BridgeError) else "io_error")
                continue

            pair = pairs_data["pairs"].get(pid_name)
            if not pair or not pair.get("enabled", True):
                self.move(path, "failed")
                self.log("inbox_task_rejected", tid, "unknown_or_disabled_pair")
                continue
            if resolve_effective_route(cfg, pair) != "gemini":
                continue
            if pid_name in active_by_pair:
                continue
            if badge_by_title.get(pair["geminiTitle"]) == "in_progress":
                continue

            cd = self._dispatch_cooldown.get(tid)
            if cd and now_mono < cd[0]:
                continue

            with file_lock(self.root / ".gemini_ui.lock"):
                ok_win, _ = drv.ensure_main_window_silent(pid)
                if not ok_win:
                    self._record_cooldown(tid, "no_main_window", cfg)
                    continue
                ok_open, open_reason = drv.open_conversation(pid, pair["geminiTitle"])
                if not ok_open:
                    self._record_cooldown(tid, open_reason, cfg)
                    continue
                poll = drv.poll_conversation(pid)
                if poll.get("window_title") != pair["geminiTitle"]:
                    self._record_cooldown(tid, "title_verification_failed", cfg)
                    continue
                if poll.get("busy") or poll.get("confirmation_card"):
                    continue

                framed = build_framed_prompt(
                    task_id=tid,
                    pair_id=pid_name,
                    reply_thread_id=reply_tid,
                    message=msg,
                    outbox_path=self.root / "outbox" / f"{tid}.json",
                )
                ok_sub, sub_reason = drv.write_prompt_and_submit(pid, framed)
                if not ok_sub:
                    self._record_cooldown(tid, sub_reason, cfg)
                    continue

            self._dispatch_cooldown.pop(tid, None)
            self.move(path, "inbox/in_flight")
            active_by_pair[pid_name] = tid
            with self.ledger_tx() as ld:
                ld["dispatches"][tid] = {
                    "status": "in_flight",
                    "pairId": pid_name,
                    "geminiTitle": pair["geminiTitle"],
                    "replyThreadId": reply_tid,
                    "replyMode": task.get("replyMode") or pair.get("replyMode", "auto"),
                    "createdAt": task.get("createdAt") or now_iso(),
                    "dispatchedAt": now_iso(),
                    "updatedAt": now_iso(),
                    "dispatchedMono": time.monotonic(),
                    "baselineTextLen": len(str(poll.get("text") or "")),
                    "baselineChunks": poll.get("chunks") or [],
                    "idleSinceMono": None,
                    "stableSinceMono": None,
                    "stableCount": 0,
                    "lastPollLen": None,
                    "notifiedAttention": False,
                }
            self.log("dispatched_to_gemini", tid, sub_reason, pairId=pid_name)

    def _record_cooldown(self, task_id: str, reason: str, cfg: dict[str, Any]) -> None:
        prev = self._dispatch_cooldown.get(task_id)
        fails = (prev[1] + 1) if prev else 1
        delay = min(60.0, 5.0 * fails)
        self._dispatch_cooldown[task_id] = (time.monotonic() + delay, fails, reason)
        if not prev or prev[2] != reason:
            self.log("dispatch_deferred", task_id, reason, attempt=fails)
        if fails == 3:
            self.notify("T3-Gemini Dispatch Deferred", f"Task {task_id} could not locate or submit to Gemini ({reason}).", cfg["notifications"])

    def scan(self, recover: bool = False) -> float:
        cfg = self.config()
        self.pairs()
        with file_lock(self.root / ".ledger.lock"):
            self.ledger()
        if recover:
            self.recover(cfg)
        probes = self.system_probes()
        self.write_health(probes, cfg)
        self.process_outbox(cfg)
        self.process_in_flight(cfg, probes)
        self.process_outbox(cfg)
        self.process_inbox(cfg, probes)
        return cfg["poll_interval_seconds"]

    def execute(self, continuous: bool) -> int:
        self.setup()
        try:
            with file_lock(self.root / ".process.lock", nonblocking=True) as fd:
                os.ftruncate(fd, 0)
                os.write(fd, str(os.getpid()).encode("ascii"))
                self.log("daemon_started" if continuous else "once_started")
                first = True
                while not self.stop:
                    delay = 2.0
                    try:
                        delay = self.scan(recover=first)
                        first = False
                        self.last_error = None
                    except Exception as exc:
                        first = True
                        code = str(exc) if isinstance(exc, BridgeError) else type(exc).__name__
                        if code != self.last_error:
                            self.log("scan_blocked", reason=code)
                            self.last_error = code
                        self.write_health(self.system_probes(), None)
                        if not continuous:
                            return 2
                    if not continuous:
                        break
                    until = time.monotonic() + delay
                    while not self.stop and time.monotonic() < until:
                        time.sleep(min(0.2, max(0.0, until - time.monotonic())))
                self.log("daemon_stopped")
                return 0
        except BlockingIOError:
            print(json.dumps({"status": "already_running_no_second_sender"}))
            return 3


def _validated_label(label: str | None = None) -> tuple[str, Path]:
    lbl = LABEL if label is None else label
    if not isinstance(lbl, str) or not LABEL_RE.fullmatch(lbl) or ".." in lbl:
        raise BridgeError("invalid_launchagent_label")
    home_dir = os.environ.get("HOME") or HOST_HOME
    plist_path = Path(home_dir) / "Library/LaunchAgents" / (lbl + ".plist")
    return lbl, plist_path


def agent_info() -> dict[str, Any]:
    if sys.platform != "darwin":
        return {"loaded": False, "reason": "non_macos"}
    try:
        label, _ = _validated_label()
    except BridgeError as exc:
        return {"loaded": False, "reason": str(exc)}
    try:
        res = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        info: dict[str, Any] = {"loaded": res.returncode == 0}
        for key, regex in (("pid", r"^\s*pid = (\d+)"), ("lastExitCode", r"^\s*last exit code = (-?\d+)")):
            m = re.search(regex, res.stdout, re.M)
            if m:
                info[key] = int(m.group(1))
        m = re.search(r"^\s*state = (\w+)", res.stdout, re.M)
        if m:
            info["state"] = m.group(1)
        return info
    except (OSError, subprocess.TimeoutExpired):
        return {"loaded": False, "reason": "launchctl_unavailable"}


def cmd_doctor(bridge: Bridge, as_json: bool) -> int:
    bridge.setup()
    cfg = bridge.config()
    pairs_data = bridge.pairs()
    probes = bridge.system_probes()
    health = bridge.read_health()
    t3_cli_ok = Path(bridge.cli_path).exists() and os.access(bridge.cli_path, os.X_OK)
    runtime_file = Path(bridge.t3_home) / "userdata/server-runtime.json"
    t3_runtime = None
    if runtime_file.exists():
        try:
            t3_runtime = decode_json(read_bytes(runtime_file, 65536))
        except Exception:
            t3_runtime = {"error": "unreadable"}
    gem_ver = None
    home_dir = os.environ.get("HOME") or HOST_HOME
    for info_plist in (
        Path("/Applications/Gemini.app/Contents/Info.plist"),
        Path(home_dir) / "Applications/Gemini.app/Contents/Info.plist",
    ):
        if info_plist.exists():
            try:
                gem_ver = plistlib.loads(info_plist.read_bytes()).get("CFBundleShortVersionString")
                break
            except Exception:
                pass
    drv = bridge.ax()
    win_title = None
    if drv is not None and probes.get("axTrusted") and probes.get("geminiPid"):
        try:
            win_title = drv.get_window_title(probes["geminiPid"])
        except Exception:
            pass

    report = {
        "python": sys.version.split()[0],
        "queueRoot": str(bridge.root),
        "globalMode": cfg["global_mode"],
        "pairCount": len(pairs_data["pairs"]),
        "currentProcess": probes,
        "geminiAppVersion": gem_ver,
        "geminiWindowTitle": win_title,
        "t3CliPath": bridge.cli_path,
        "t3CliExecutable": t3_cli_ok,
        "t3ServerRuntime": t3_runtime,
        "launchAgent": agent_info(),
        "daemonHealth": health,
    }
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Python: {report['python']} | Root: {report['queueRoot']}")
        print(f"Global Mode: {report['globalMode']} | Pairs: {report['pairCount']}")
        print(f"Gemini: running={probes['geminiRunning']} pid={probes['geminiPid']} ver={gem_ver} win={win_title!r}")
        print(f"Current AXTrusted: {probes['axTrusted']} | ScreenLocked: {probes['screenLocked']}")
        print(f"T3 CLI: {bridge.cli_path} (executable={t3_cli_ok})")
        print(f"LaunchAgent: {report['launchAgent']}")
        print(f"DaemonHealth: {health}")
    return 0


def cmd_status(bridge: Bridge, as_json: bool) -> int:
    bridge.setup()
    counts: dict[str, int] = {}
    for name in DIRS:
        p = bridge.root / name
        counts[name] = sum(1 for x in p.iterdir() if x.is_file() and not x.name.startswith((".", "_"))) if p.is_dir() else 0
    info: dict[str, Any] = {
        "agent": agent_info(),
        "queueRoot": str(bridge.root),
        "counts": counts,
        "daemonHealth": bridge.read_health(),
    }
    try:
        cfg = bridge.config()
        pairs_data = bridge.pairs()
        ledger_data = bridge.ledger()
        in_flight = bridge._active_in_flight_by_pair()
        pair_summary = []
        for pid, p in sorted(pairs_data["pairs"].items()):
            pair_summary.append({
                "pairId": pid,
                "label": p.get("label"),
                "t3ThreadId": p["t3ThreadId"],
                "defaultReplyThreadId": p["defaultReplyThreadId"],
                "geminiTitle": p["geminiTitle"],
                "configuredRoute": p["route"],
                "effectiveRoute": resolve_effective_route(cfg, p),
                "replyMode": p.get("replyMode", "auto"),
                "enabled": p.get("enabled", True),
                "inFlightTaskId": in_flight.get(pid),
            })
        info.update(
            globalMode=cfg["global_mode"],
            pollIntervalSeconds=cfg["poll_interval_seconds"],
            pairs=pair_summary,
            taskStates={k: sum(v.get("status") == k for v in ledger_data["tasks"].values()) for k in ("sending", "sent", "unknown_timeout", "failed")},
            dispatchStates={k: sum(v.get("status") == k for v in ledger_data["dispatches"].values()) for k in ("in_flight", "needs_attention", "captured_to_outbox", "completed", "timed_out", "failed")},
        )
    except Exception as exc:
        info["blocked"] = str(exc) if isinstance(exc, BridgeError) else type(exc).__name__
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0 if "blocked" not in info else 2


def cmd_mode(bridge: Bridge, mode_name: str) -> int:
    bridge.setup()
    cfg = bridge.config()
    if mode_name == "t3-all":
        cfg["global_mode"] = "t3_only"
        bridge.save_config(cfg)
    elif mode_name == "hybrid":
        cfg["global_mode"] = "hybrid"
        bridge.save_config(cfg)
    elif mode_name == "gemini-all":
        cfg["global_mode"] = "hybrid"
        bridge.save_config(cfg)
        with bridge.pairs_tx() as pdata:
            for p in pdata["pairs"].values():
                if p.get("enabled", True):
                    p["route"] = "gemini"
                    p["updatedAt"] = now_iso()
    else:
        raise BridgeError("invalid_mode")
    bridge.log("mode_changed", reason=mode_name)
    print(json.dumps({"ok": True, "mode": mode_name, "global_mode": cfg["global_mode"]}, ensure_ascii=False))
    return 0


def cmd_pair(bridge: Bridge, args: argparse.Namespace) -> int:
    bridge.setup()
    if args.pair_cmd == "list":
        cfg = bridge.config()
        pdata = bridge.pairs()
        out = []
        for pid, p in sorted(pdata["pairs"].items()):
            item = dict(p)
            item["effectiveRoute"] = resolve_effective_route(cfg, p)
            out.append(item)
        print(json.dumps({"globalMode": cfg["global_mode"], "pairs": out}, ensure_ascii=False, indent=2))
        return 0

    if args.pair_cmd == "gemini-chats":
        probes = bridge.system_probes()
        if not probes.get("axTrusted"):
            raise BridgeError("ax_not_trusted")
        pid = probes.get("geminiPid")
        if not pid:
            raise BridgeError("gemini_not_running")
        drv = bridge.ax()
        print(json.dumps(drv.list_conversations(pid), ensure_ascii=False, indent=2))
        return 0

    if args.pair_cmd == "add":
        if not PAIR_ID_RE.fullmatch(args.id):
            raise BridgeError("invalid_pair_id")
        if not THREAD_ID_RE.fullmatch(args.t3_thread):
            raise BridgeError("invalid_t3_thread_id")
        reply_to = args.reply_to or args.t3_thread
        if not THREAD_ID_RE.fullmatch(reply_to):
            raise BridgeError("invalid_reply_thread_id")
        ws = str(Path(args.workspace).expanduser().resolve()) if args.workspace else None
        with bridge.pairs_tx() as pdata:
            pdata["pairs"][args.id] = {
                "pairId": args.id,
                "label": args.label or args.id,
                "t3ThreadId": args.t3_thread,
                "defaultReplyThreadId": reply_to,
                "allowedReplyThreadIds": sorted({args.t3_thread, reply_to}),
                "geminiTitle": args.gemini_title.strip(),
                "workspacePath": ws,
                "route": args.route,
                "replyMode": args.reply_mode,
                "enabled": True,
                "createdAt": pdata["pairs"].get(args.id, {}).get("createdAt") or now_iso(),
                "updatedAt": now_iso(),
            }
        print(json.dumps({"ok": True, "pair": pdata["pairs"][args.id]}, ensure_ascii=False, indent=2))
        return 0

    if args.pair_cmd == "init":
        if not PAIR_ID_RE.fullmatch(args.id):
            raise BridgeError("invalid_pair_id")
        if not THREAD_ID_RE.fullmatch(args.t3_thread):
            raise BridgeError("invalid_t3_thread_id")
        reply_to = args.reply_to or args.t3_thread
        if not THREAD_ID_RE.fullmatch(reply_to):
            raise BridgeError("invalid_reply_thread_id")
        probes = bridge.system_probes()
        if not probes.get("axTrusted"):
            raise BridgeError("ax_not_trusted")
        pid = probes.get("geminiPid")
        if not pid:
            raise BridgeError("gemini_not_running")
        ws = str(Path(args.workspace).expanduser().resolve()) if args.workspace else None
        # Ensure uniqueness before creating conversation
        existing = bridge.pairs()
        for k, v in existing["pairs"].items():
            if k != args.id and v.get("enabled", True):
                if v.get("t3ThreadId") == args.t3_thread:
                    raise BridgeError("duplicate_t3_thread_in_pairs")
                if v.get("geminiTitle") == args.gemini_title.strip():
                    raise BridgeError("duplicate_gemini_title_in_pairs")

        init_prompt = args.init_prompt or (
            f"This is a dedicated 1-to-1 T3 Code bridge task conversation (pairId={args.id}). "
            f"Do not modify any files. Reply only with: READY ({args.gemini_title})"
        )
        drv = bridge.ax()
        with file_lock(bridge.root / ".gemini_ui.lock"):
            ok, actual_title, mode_detail = drv.create_task_conversation(
                pid=pid,
                target_title=args.gemini_title.strip(),
                init_prompt=init_prompt,
                workspace_path=ws,
                wait_s=float(args.wait_sec),
            )
        if not ok:
            raise BridgeError(f"gemini_init_failed:{actual_title}")
        with bridge.pairs_tx() as pdata:
            pdata["pairs"][args.id] = {
                "pairId": args.id,
                "label": args.label or args.id,
                "t3ThreadId": args.t3_thread,
                "defaultReplyThreadId": reply_to,
                "allowedReplyThreadIds": sorted({args.t3_thread, reply_to}),
                "geminiTitle": actual_title,
                "workspacePath": ws,
                "route": "gemini",
                "replyMode": args.reply_mode,
                "enabled": True,
                "createdAt": pdata["pairs"].get(args.id, {}).get("createdAt") or now_iso(),
                "updatedAt": now_iso(),
            }
        print(json.dumps({"ok": True, "titleMode": mode_detail, "pair": pdata["pairs"][args.id]}, ensure_ascii=False, indent=2))
        return 0

    if args.pair_cmd == "route":
        with bridge.pairs_tx() as pdata:
            if args.id not in pdata["pairs"]:
                raise BridgeError("unknown_pair_id")
            pdata["pairs"][args.id]["route"] = args.route
            pdata["pairs"][args.id]["updatedAt"] = now_iso()
        print(json.dumps({"ok": True, "pairId": args.id, "route": args.route}, ensure_ascii=False))
        return 0

    if args.pair_cmd == "remove":
        in_flight = bridge._active_in_flight_by_pair()
        if args.id in in_flight and not args.force:
            raise BridgeError("pair_has_in_flight_task")
        with bridge.pairs_tx() as pdata:
            if args.id not in pdata["pairs"]:
                raise BridgeError("unknown_pair_id")
            removed = pdata["pairs"].pop(args.id)
        print(json.dumps({"ok": True, "removed": removed["pairId"]}, ensure_ascii=False))
        return 0

    raise BridgeError("unknown_pair_subcommand")


def cmd_send(bridge: Bridge, args: argparse.Namespace) -> int:
    bridge.setup()
    cfg = bridge.config()
    pairs_data = bridge.pairs()
    if args.pair not in pairs_data["pairs"]:
        raise BridgeError("unknown_pair_id")
    pair = pairs_data["pairs"][args.pair]
    if not pair.get("enabled", True):
        raise BridgeError("pair_disabled")

    sources = sum(bool(x) for x in (args.message is not None, args.file is not None, args.stdin))
    if sources != 1:
        raise BridgeError("specify_exactly_one_message_source")
    if args.message is not None:
        message = args.message
    elif args.file is not None:
        message = Path(args.file).read_text(encoding="utf-8")
    else:
        message = sys.stdin.read()

    if not message or not message.strip() or "\x00" in message:
        raise BridgeError("invalid_message")
    if len(message.encode("utf-8")) > cfg["max_message_bytes"] or len(message.splitlines()) > cfg["max_message_lines"]:
        raise BridgeError("message_limit_exceeded")

    task_id = args.task_id or make_task_id()
    if not TASK_ID_RE.fullmatch(task_id):
        raise BridgeError("invalid_task_id")

    reply_to = args.reply_to or pair["defaultReplyThreadId"]
    if not THREAD_ID_RE.fullmatch(reply_to) or reply_to not in _allowed_reply_targets_for_pair(pair, pairs_data):
        raise BridgeError("reply_target_not_whitelisted")

    effective = resolve_effective_route(cfg, pair)
    if effective == "t3":
        res = bridge.deliver_to_t3_cli(pair["t3ThreadId"], message)
        with bridge.ledger_tx() as ld:
            ld["dispatches"][task_id] = {
                "status": "direct_t3",
                "pairId": args.pair,
                "t3ThreadId": pair["t3ThreadId"],
                "createdAt": now_iso(),
                "updatedAt": now_iso(),
            }
        bridge.log("sent_direct_t3", task_id, pairId=args.pair)
        print(json.dumps({"ok": True, "routed": "t3", "taskId": task_id, "pairId": args.pair, "threadId": pair["t3ThreadId"], **res}, ensure_ascii=False, indent=2))
        return 0

    # Route to Gemini queue
    with file_lock(bridge.root / ".ledger.lock"):
        ld_snap = bridge.ledger()
        if task_id in ld_snap["tasks"] or task_id in ld_snap["dispatches"]:
            raise BridgeError("duplicate_task_id")

    envelope = {
        "schemaVersion": 2,
        "taskId": task_id,
        "pairId": args.pair,
        "replyThreadId": reply_to,
        "replyMode": pair.get("replyMode", "auto"),
        "message": message,
        "createdAt": now_iso(),
    }
    inbox_path = bridge.root / "inbox" / f"{task_id}.task.json"
    atomic_json(inbox_path, envelope)
    bridge.log("queued_for_gemini", task_id, pairId=args.pair)

    if not args.wait:
        print(json.dumps({
            "ok": True,
            "routed": "gemini",
            "queued": True,
            "taskId": task_id,
            "pairId": args.pair,
            "geminiTitle": pair["geminiTitle"],
            "replyThreadId": reply_to,
            "inboxFile": str(inbox_path),
        }, ensure_ascii=False, indent=2))
        return 0

    deadline = time.monotonic() + max(5.0, float(args.timeout))
    while time.monotonic() < deadline:
        with file_lock(bridge.root / ".ledger.lock"):
            ld = bridge.ledger()
        t_rec = ld["tasks"].get(task_id)
        d_rec = ld["dispatches"].get(task_id)
        if t_rec and t_rec.get("status") == "sent":
            print(json.dumps({
                "ok": True,
                "routed": "gemini",
                "taskId": task_id,
                "pairId": args.pair,
                "status": "sent",
                "receipt": t_rec.get("receipt"),
            }, ensure_ascii=False, indent=2))
            return 0
        if t_rec and t_rec.get("status") in ("failed", "unknown_timeout"):
            print(json.dumps({
                "ok": False,
                "routed": "gemini",
                "taskId": task_id,
                "status": t_rec.get("status"),
                "reason": t_rec.get("reason"),
            }, ensure_ascii=False, indent=2))
            return 2
        if d_rec and d_rec.get("status") in ("failed", "timed_out"):
            print(json.dumps({
                "ok": False,
                "routed": "gemini",
                "taskId": task_id,
                "status": d_rec.get("status"),
                "reason": d_rec.get("reason"),
            }, ensure_ascii=False, indent=2))
            return 2
        time.sleep(0.5)

    print(json.dumps({"ok": False, "routed": "gemini", "taskId": task_id, "status": "wait_timeout"}, ensure_ascii=False, indent=2))
    return 1


def _parse_version_tuple(version_str: str) -> tuple[int, ...]:
    parts: list[int] = []
    for tok in re.split(r"[^0-9]+", version_str):
        if tok:
            parts.append(int(tok))
    return tuple(parts)


def _discover_framework_python_apps(base_glob_parent: Path) -> list[Path]:
    if not base_glob_parent.is_dir():
        return []
    cur_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    matches = list(base_glob_parent.glob("*/Resources/Python.app/Contents/MacOS/Python"))
    matches.sort(
        key=lambda p: (
            p.parents[4].name == cur_ver if len(p.parents) > 4 else False,
            _parse_version_tuple(p.parents[4].name) if len(p.parents) > 4 else (),
            str(p),
        ),
        reverse=True,
    )
    return matches


def resolve_launchagent_python() -> str:
    """Resolve a dedicated single-link Python executable so macOS TCC never attributes launchd jobs to /usr/bin/git."""
    candidates: list[Path] = [
        Path(sys.base_prefix) / "Resources/Python.app/Contents/MacOS/Python",
    ]
    for fw_versions in (
        Path("/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions"),
        Path("/Library/Frameworks/Python.framework/Versions"),
        Path("/opt/homebrew/Frameworks/Python.framework/Versions"),
        Path("/usr/local/Frameworks/Python.framework/Versions"),
    ):
        candidates.extend(_discover_framework_python_apps(fw_versions))

    seen: set[str] = set()
    for cand in candidates:
        s = str(cand)
        if s in seen:
            continue
        seen.add(s)
        try:
            if cand.is_file() and os.access(s, os.X_OK) and cand.stat().st_nlink == 1:
                return s
        except OSError:
            continue

    exe = os.path.realpath(sys.executable)
    try:
        if os.access(exe, os.X_OK) and os.stat(exe).st_nlink == 1:
            return exe
    except OSError:
        pass
    return shutil.which("python3") or "/usr/bin/python3"


def install_agent(bridge: Bridge) -> int:
    bridge.setup()
    label, plist_path = _validated_label()
    no_symlink(plist_path)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    if plist_path.exists():
        old = plistlib.loads(read_bytes(plist_path, 65536))
        if old.get("Label") != label:
            raise BridgeError("refusing_unrelated_launchagent")
    py_exec = resolve_launchagent_python()
    home_dir = os.environ.get("HOME") or HOST_HOME
    env_vars = {
        "HOME": home_dir,
        "PATH": child_env()["PATH"],
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "T3_GEMINI_BRIDGE_ROOT": str(bridge.root),
    }
    if label != "io.t3-gemini-bridge.daemon" or os.environ.get("T3_GEMINI_BRIDGE_LABEL"):
        env_vars["T3_GEMINI_BRIDGE_LABEL"] = label
    if os.environ.get("T3_GEMINI_BRIDGE_CLI"):
        env_vars["T3_GEMINI_BRIDGE_CLI"] = os.environ["T3_GEMINI_BRIDGE_CLI"]
    if bridge.t3_home != str(Path(home_dir) / ".t3") or os.environ.get("T3_GEMINI_BRIDGE_T3_HOME"):
        env_vars["T3_GEMINI_BRIDGE_T3_HOME"] = bridge.t3_home

    data = {
        "Label": label,
        "ProgramArguments": [py_exec, "-B", str(SCRIPT), "--root", str(bridge.root), "run"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "WorkingDirectory": str(bridge.root),
        "Umask": 0o077,
        "EnvironmentVariables": env_vars,
        "StandardOutPath": str(bridge.root / "logs/launchagent.stdout.log"),
        "StandardErrorPath": str(bridge.root / "logs/launchagent.stderr.log"),
    }
    if agent_info()["loaded"]:
        subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True, timeout=15)
    fd = os.open(str(plist_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(plistlib.dumps(data))
        stream.flush()
        os.fsync(stream.fileno())
    p = subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)], capture_output=True, timeout=15)
    if p.returncode != 0:
        raise BridgeError("launchagent_bootstrap_failed")
    print(json.dumps({"installed": str(plist_path), "label": label, "pythonExecutable": py_exec, "agent": agent_info()}, ensure_ascii=False))
    return 0


def uninstall_agent() -> int:
    label, plist_path = _validated_label()
    info = agent_info()
    if plist_path.exists():
        no_symlink(plist_path)
        old = plistlib.loads(read_bytes(plist_path, 65536))
        if old.get("Label") != label:
            raise BridgeError("refusing_unrelated_launchagent")
    if info["loaded"]:
        p = subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True, timeout=15)
        if p.returncode != 0:
            raise BridgeError("launchagent_bootout_failed")
    if plist_path.exists():
        plist_path.unlink()
    print(json.dumps({"uninstalled": True, "label": label, "dataPreserved": True}))
    return 0


def restart_agent() -> int:
    label, _ = _validated_label()
    p = subprocess.run(["/bin/launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"], capture_output=True, text=True, timeout=15)
    if p.returncode != 0:
        raise BridgeError("launchagent_kickstart_failed")
    print(json.dumps({"restarted": True, "label": label, "agent": agent_info()}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="Bridge root directory")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_doc = sub.add_parser("doctor", help="Check AX, Gemini.app, T3 CLI, and LaunchAgent health")
    p_doc.add_argument("--json", action="store_true")

    p_st = sub.add_parser("status", help="Show bridge queue and pair routing status")
    p_st.add_argument("--json", action="store_true")

    p_mode = sub.add_parser("mode", help="Switch global mode (hybrid / t3-all / gemini-all)")
    p_mode.add_argument("target_mode", choices=("hybrid", "t3-all", "gemini-all"))

    p_pair = sub.add_parser("pair", help="Manage 1-to-1 T3 <-> Gemini conversation pairs")
    pair_sub = p_pair.add_subparsers(dest="pair_cmd", required=True)

    p_pl = pair_sub.add_parser("list", help="List configured 1-to-1 pairs")
    p_pl.add_argument("--json", action="store_true")

    p_gc = pair_sub.add_parser("gemini-chats", help="List conversations currently visible in Gemini.app")
    p_gc.add_argument("--json", action="store_true")

    p_pa = pair_sub.add_parser("add", help="Bind an existing T3 thread to an existing Gemini conversation title")
    p_pa.add_argument("--id", required=True)
    p_pa.add_argument("--t3-thread", required=True)
    p_pa.add_argument("--gemini-title", required=True)
    p_pa.add_argument("--reply-to")
    p_pa.add_argument("--label")
    p_pa.add_argument("--workspace")
    p_pa.add_argument("--route", choices=("gemini", "t3"), default="gemini")
    p_pa.add_argument("--reply-mode", choices=("auto", "outbox", "ax_capture"), default="auto")

    p_pi = pair_sub.add_parser("init", help="Create a new Gemini Task conversation and bind 1-to-1")
    p_pi.add_argument("--id", required=True)
    p_pi.add_argument("--t3-thread", required=True)
    p_pi.add_argument("--gemini-title", required=True)
    p_pi.add_argument("--reply-to")
    p_pi.add_argument("--label")
    p_pi.add_argument("--workspace")
    p_pi.add_argument("--init-prompt")
    p_pi.add_argument("--reply-mode", choices=("auto", "outbox", "ax_capture"), default="auto")
    p_pi.add_argument("--wait-sec", type=float, default=35.0)

    p_pr = pair_sub.add_parser("route", help="Switch a single pair between t3 and gemini")
    p_pr.add_argument("id")
    p_pr.add_argument("route", choices=("t3", "gemini"))

    p_prm = pair_sub.add_parser("remove", help="Remove a pair mapping")
    p_prm.add_argument("id")
    p_prm.add_argument("--force", action="store_true")

    p_send = sub.add_parser("send", help="Send a task via configured 1-to-1 pair (auto routes to Gemini or T3)")
    p_send.add_argument("--pair", required=True)
    p_send.add_argument("--message")
    p_send.add_argument("--file")
    p_send.add_argument("--stdin", action="store_true")
    p_send.add_argument("--task-id")
    p_send.add_argument("--reply-to")
    p_send.add_argument("--wait", action="store_true")
    p_send.add_argument("--timeout", type=float, default=300.0)
    p_send.add_argument("--json", action="store_true")

    sub.add_parser("run", help="Run continuous bridge daemon")
    sub.add_parser("once", help="Run a single bridge scan cycle")

    p_svc = sub.add_parser("service", help="Manage LaunchAgent background daemon")
    p_svc.add_argument("svc_cmd", choices=("install", "uninstall", "restart", "status"))

    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    parser = build_parser()
    # Allow `--message -` to mean stdin cleanly
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    normalized_argv: list[str] = []
    i = 0
    while i < len(raw_argv):
        tok = raw_argv[i]
        if tok == "--message" and i + 1 < len(raw_argv) and raw_argv[i + 1] == "-":
            normalized_argv.append("--stdin")
            i += 2
            continue
        normalized_argv.append(tok)
        i += 1

    args = parser.parse_args(normalized_argv)
    bridge = Bridge(root=Path(args.root))
    signal.signal(signal.SIGTERM, lambda *_: setattr(bridge, "stop", True))
    signal.signal(signal.SIGINT, lambda *_: setattr(bridge, "stop", True))

    try:
        if args.cmd == "doctor":
            return cmd_doctor(bridge, args.json)
        if args.cmd == "status":
            return cmd_status(bridge, args.json)
        if args.cmd == "mode":
            return cmd_mode(bridge, args.target_mode)
        if args.cmd == "pair":
            return cmd_pair(bridge, args)
        if args.cmd == "send":
            return cmd_send(bridge, args)
        if args.cmd == "run":
            return bridge.execute(continuous=True)
        if args.cmd == "once":
            return bridge.execute(continuous=False)
        if args.cmd == "service":
            if args.svc_cmd == "install":
                return install_agent(bridge)
            if args.svc_cmd == "uninstall":
                return uninstall_agent()
            if args.svc_cmd == "restart":
                return restart_agent()
            if args.svc_cmd == "status":
                print(json.dumps({"agent": agent_info(), "daemonHealth": bridge.read_health()}, ensure_ascii=False, indent=2))
                return 0
        return 2
    except Exception as exc:
        err = str(exc) if isinstance(exc, BridgeError) else f"{type(exc).__name__}:{exc}"
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
