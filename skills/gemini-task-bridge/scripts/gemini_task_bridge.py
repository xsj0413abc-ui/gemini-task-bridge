#!/usr/bin/python3
"""T3 Code / Multi-Agent <-> Gemini Desktop App 1-to-1 automatic bidirectional bridge.

Python 3.9+ standard library only; no T3 database/token manipulation.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import ctypes.util
import datetime
import errno
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

BRIDGE_VERSION = "2.2.0"
BRIDGE_CAPABILITIES = ("local_adapter", "task_reservations", "sent_payload_store", "daemon_control_plane")

LEDGER_HARD_MAX_BYTES = 32 * 1024 * 1024
LEDGER_INTAKE_MAX_BYTES = 28 * 1024 * 1024
LEDGER_WARN_BYTES = 24 * 1024 * 1024
MAX_BASELINE_BYTES = 64 * 1024
MAX_BASELINE_CHUNKS = 200
IN_FLIGHT_ENVELOPE_READ_MAX_BYTES = 2 * 1024 * 1024
ARCHIVED_PAYLOAD_READ_MAX_BYTES = 2 * 1024 * 1024 + 65536

LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PAIR_UID_RE = re.compile(r"^[0-9a-f]{32}$")
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
MCP_SCOPE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

HOST_HOME = os.environ.get("HOME") or str(Path.home())
DEFAULT_LABEL = "io.gemini-task-bridge.daemon"


def bridge_env(name: str, default: str | None = None, *, environ=None) -> str | None:
    env = os.environ if environ is None else environ
    return env.get("GEMINI_TASK_BRIDGE_" + name, env.get("T3_GEMINI_BRIDGE_" + name, default))


def resolve_default_root() -> Path:
    override = bridge_env("ROOT")
    if override:
        return Path(override)
    share = Path(os.environ.get("HOME") or HOST_HOME) / ".local/share"
    modern, legacy = share / "gemini-task-bridge", share / "t3-gemini-bridge"
    return legacy if not modern.is_dir() and legacy.is_dir() else modern


def resolve_default_t3_home() -> str:
    return bridge_env("T3_HOME") or str(Path(os.environ.get("HOME") or HOST_HOME) / ".t3")


def resolve_default_t3_cli(t3_home: str | None = None) -> str:
    """Resolve the `t3` CLI binary via env override, T3 home bin, or PATH."""
    env_cli = (bridge_env("CLI") or "").strip()
    if env_cli:
        return env_cli
    if t3_home is None:
        t3_home = resolve_default_t3_home()
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


DIRS = (
    "inbox",
    "inbox/in_flight",
    "inbox/done",
    "outbox",
    "sending",
    "sent",
    "sent/payloads",
    "failed",
    "dedup",
    "logs",
    "locks",
)

TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
PAIR_ID_RE = re.compile(r"^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff_.-]{0,63}$")
THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,255}$")

SENTINEL_START_PREFIX = "[[T3_BRIDGE_START:"
SENTINEL_DONE_PREFIX = "[[T3_BRIDGE_DONE:"
SENTINEL_SUFFIX = "]]"

ACTIVE_DISPATCH_STATUSES = (
    "queued",
    "dispatching",
    "in_flight",
    "needs_attention",
    "captured_to_outbox",
    "sending",
    "direct_t3_sending",
)
TERMINAL_DISPATCH_STATUSES = (
    "direct_t3",
    "completed",
    "timed_out",
    "failed",
    "unknown_timeout",
)
OUTBOX_RECEIPT_ALLOWED_STATUSES = (
    "dispatching",
    "in_flight",
    "needs_attention",
    "captured_to_outbox",
)
TRANSIENT_LEDGER_FIELDS = (
    "baselineChunks",
    "idleSinceMono",
    "stableSinceMono",
    "stableCount",
    "lastPollLen",
)
PRE_SUBMIT_REVERT_REASONS = frozenset({
    "empty_prompt",
    "no_main_window",
    "composer_not_found",
    "ax_set_value_failed",
    "ax_value_verify_failed",
    "send_button_not_found_or_disabled",
})

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


_UNSET: Any = object()


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def parse_iso_utc(raw: Any) -> datetime.datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _parse_version_tuple(version_str: str) -> tuple[int, ...]:
    parts: list[int] = []
    for tok in re.split(r"[^0-9]+", version_str):
        if tok:
            parts.append(int(tok))
    return tuple(parts)


def make_task_id() -> str:
    ts = datetime.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return f"{ts}-{uuid.uuid4().hex[:6]}"


def is_reserved_task_id(task_id: str) -> bool:
    """Reject new task IDs ending with .receipt (case-insensitive) to avoid flat sent/ receipt ambiguity."""
    return task_id.lower().endswith(".receipt")


def sha256_text(text: str) -> tuple[str, int]:
    raw = text.encode("utf-8")
    return hashlib.sha256(raw).hexdigest(), len(raw)


def bound_baseline_chunks(chunks: list[str] | None) -> list[str]:
    """Keep the trailing window of non-empty baseline chunks within strict size/count budgets."""
    if not chunks:
        return []
    cleaned = [c for c in chunks if isinstance(c, str) and c]
    if not cleaned:
        return []
    tail = cleaned[-MAX_BASELINE_CHUNKS:]
    kept: list[str] = []
    total_bytes = 0
    for item in reversed(tail):
        b = len(item.encode("utf-8"))
        if total_bytes + b > MAX_BASELINE_BYTES:
            break
        kept.append(item)
        total_bytes += b
    kept.reverse()
    return kept


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


def read_bytes(path: Path, limit: int, *, private: bool = False) -> bytes:
    no_symlink(path)
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if private:
            _check_private_file(info)
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BridgeError("non_regular_or_hardlinked_file")
        if info.st_size > limit:
            raise BridgeError("file_too_large")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        after = os.fstat(fd)
        if private:
            _check_private_file(after)
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


def encode_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def atomic_json_bytes(path: Path, data: bytes) -> None:
    no_symlink(path)
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


def atomic_json(path: Path, value: Any) -> None:
    atomic_json_bytes(path, encode_json_bytes(value))


def _check_private_file(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise BridgeError("non_regular_or_hardlinked_file")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise BridgeError("control_plane_unavailable")


@contextlib.contextmanager
def file_lock(path: Path, nonblocking: bool = False, *, private: bool = False):
    no_symlink(path)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if private:
            _check_private_file(os.fstat(fd))
        fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        yield fd
    finally:
        os.close(fd)


def _load_libc() -> Any:
    c_path = ctypes.util.find_library("c")
    if not c_path:
        if sys.platform == "darwin":
            c_path = "/usr/lib/system/libsystem_c.dylib"
        else:
            c_path = "libc.so.6"
    return ctypes.CDLL(c_path, use_errno=True)


def atomic_rename_excl(
    src: Path, dst: Path, lock_dir: Path | None = None, *, private_lock: bool = False,
) -> None:
    """Atomically publish src -> dst without overwriting existing dst and keeping st_nlink == 1."""
    no_symlink(src)
    no_symlink(dst)
    src_b = os.fsencode(src)
    dst_b = os.fsencode(dst)
    fallback_errnos = (errno.ENOTSUP, errno.ENOSYS, errno.EINVAL)
    try:
        libc = _load_libc()
    except OSError:
        libc = None

    if libc is not None and sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        fn = libc.renamex_np
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        ctypes.set_errno(0)
        rc = int(fn(src_b, dst_b, 0x00000004))  # RENAME_EXCL
        if rc == 0:
            fsync_dir(dst.parent)
            return
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise FileExistsError(err, os.strerror(err), str(dst))
        if err not in fallback_errnos:
            raise OSError(err, os.strerror(err), str(src))
    elif libc is not None and sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        fn = libc.renameat2
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        ctypes.set_errno(0)
        rc = int(fn(-100, src_b, -100, dst_b, 1))  # AT_FDCWD, RENAME_NOREPLACE
        if rc == 0:
            fsync_dir(dst.parent)
            return
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise FileExistsError(err, os.strerror(err), str(dst))
        if err not in fallback_errnos:
            raise OSError(err, os.strerror(err), str(src))

    lock_path = (lock_dir or dst.parent) / ".inbox_publish.lock"
    with file_lock(lock_path, private=private_lock):
        if os.path.lexists(str(dst)):
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(dst))
        os.replace(str(src), str(dst))
        fsync_dir(dst.parent)


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
    reply_thread_id: str | None,
    message: str,
    outbox_path: Path,
    adapter: str = "t3",
) -> str:
    """Build a prompt wrapped with split-literal sentinels so prompt echoes never trigger completion."""
    start_tag = f"{SENTINEL_START_PREFIX}{task_id}{SENTINEL_SUFFIX}"
    if adapter == "local":
        header = f"[Bridge->Gemini Auto-Dispatch] taskId={task_id} | pairId={pair_id} | adapter=local"
    else:
        header = f"[T3->Gemini Auto-Dispatch] taskId={task_id} | pairId={pair_id} | replyThreadId={reply_thread_id}"
    # Deliberately split done prefix and task_id suffix in instructions (Prompt-Echo Immunity).
    return (
        f"{start_tag}\n"
        f"{header}\n"
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


def pair_adapter(entry: dict[str, Any]) -> str:
    val = entry.get("adapter")
    return "t3" if val is None else str(val)


def pair_scope_allowed(
    pair_id: str, pair: dict[str, Any], scope: str | None, allow_pairs: set[str] | frozenset[str] | None,
) -> bool:
    if scope is None:
        return allow_pairs is None or pair_id in allow_pairs
    return pair.get("ownerScope") == scope or (allow_pairs is not None and pair_id in allow_pairs)


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
        adapter = entry.get("adapter", "t3")
        if adapter not in ("t3", "local"):
            raise BridgeError("invalid_pair_adapter")
        pair_uid = entry.get("pairUid")
        if pair_uid is not None and (not isinstance(pair_uid, str) or not PAIR_UID_RE.fullmatch(pair_uid)):
            raise BridgeError("invalid_pair_uid")

        owner = entry.get("ownerScope")
        if owner is not None and (not isinstance(owner, str) or not MCP_SCOPE_RE.fullmatch(owner)):
            raise BridgeError("invalid_mcp_scope")
        if "createdVia" in entry and entry["createdVia"] not in ("cli", "mcp", "daemon"):
            raise BridgeError("invalid_pair_entry")
        if "controlRequestId" in entry and (
            not isinstance(entry["controlRequestId"], str) or not PAIR_UID_RE.fullmatch(entry["controlRequestId"])
        ):
            raise BridgeError("invalid_pair_entry")

        t3_id = entry.get("t3ThreadId")
        rep_id = entry.get("defaultReplyThreadId")
        gem_title = entry.get("geminiTitle")
        route = entry.get("route")
        reply_mode = entry.get("replyMode", "auto")
        enabled = entry.get("enabled", True)
        allowed_replies = entry.get("allowedReplyThreadIds")

        if adapter == "local":
            if t3_id is not None or rep_id is not None:
                raise BridgeError("local_adapter_forbids_t3_thread")
            if allowed_replies is not None and (not isinstance(allowed_replies, list) or len(allowed_replies) > 0):
                raise BridgeError("local_adapter_forbids_reply_allowlist")
            if route != "gemini":
                raise BridgeError("local_pair_has_no_t3_route")
        else:
            if not isinstance(t3_id, str) or not THREAD_ID_RE.fullmatch(t3_id):
                raise BridgeError("invalid_pair_t3_thread_id")
            if not isinstance(rep_id, str) or not THREAD_ID_RE.fullmatch(rep_id):
                raise BridgeError("invalid_pair_reply_thread_id")
            if route not in ("gemini", "t3"):
                raise BridgeError("invalid_pair_route")
            if allowed_replies is not None:
                if not isinstance(allowed_replies, list) or any(
                    not isinstance(x, str) or not THREAD_ID_RE.fullmatch(x) for x in allowed_replies
                ):
                    raise BridgeError("invalid_allowed_reply_threads")

        if not isinstance(gem_title, str) or not gem_title.strip():
            raise BridgeError("invalid_pair_gemini_title")
        if reply_mode not in ("auto", "outbox", "ax_capture"):
            raise BridgeError("invalid_pair_reply_mode")
        if not isinstance(enabled, bool):
            raise BridgeError("invalid_pair_enabled")
        if enabled:
            if adapter == "t3":
                if t3_id in seen_t3:
                    raise BridgeError("duplicate_t3_thread_in_pairs")
                seen_t3[t3_id] = pid
            if gem_title.strip() in seen_gem:
                raise BridgeError("duplicate_gemini_title_in_pairs")
            seen_gem[gem_title.strip()] = pid
    return data


def resolve_effective_route(config: dict[str, Any], pair: dict[str, Any]) -> str:
    if pair_adapter(pair) == "local":
        if config.get("global_mode") == "t3_only":
            return "paused"
        return "gemini"
    if config.get("global_mode") == "t3_only":
        return "t3"
    if pair.get("route") == "t3":
        return "t3"
    return "gemini"


def _allowed_reply_targets_for_pair(pair: dict[str, Any], pairs_data: dict[str, Any]) -> set[str]:
    if pair_adapter(pair) != "t3":
        return set()
    targets: set[str] = set()
    if isinstance(pair.get("t3ThreadId"), str):
        targets.add(pair["t3ThreadId"])
    if isinstance(pair.get("defaultReplyThreadId"), str):
        targets.add(pair["defaultReplyThreadId"])
    for item in pair.get("allowedReplyThreadIds") or []:
        targets.add(item)
    for other in pairs_data.get("pairs", {}).values():
        if (
            isinstance(other, dict)
            and other.get("enabled", True)
            and pair_adapter(other) == "t3"
            and isinstance(other.get("t3ThreadId"), str)
        ):
            targets.add(other["t3ThreadId"])
    return targets


class DeliveryAdapter:
    name: str = ""
    requires_t3_cli: bool = False

    def deliver(self, bridge: Bridge, task_id: str, resolved_target: str | None, message: str) -> dict[str, Any]:
        raise NotImplementedError


class T3DeliveryAdapter(DeliveryAdapter):
    name = "t3"
    requires_t3_cli = True

    def deliver(self, bridge: Bridge, task_id: str, resolved_target: str | None, message: str) -> dict[str, Any]:
        if not isinstance(resolved_target, str) or not THREAD_ID_RE.fullmatch(resolved_target):
            raise BridgeError("invalid_target_thread_id")
        res = bridge.deliver_to_t3_cli(resolved_target, message)
        return {
            "deliveryAdapter": "t3",
            "durationMs": res["durationMs"],
            "t3": res["t3"],
        }


class LocalDeliveryAdapter(DeliveryAdapter):
    name = "local"
    requires_t3_cli = False

    def deliver(self, bridge: Bridge, task_id: str, resolved_target: str | None, message: str) -> dict[str, Any]:
        if resolved_target is not None:
            raise BridgeError("local_adapter_forbids_t3_thread")
        if not message or not message.strip() or "\x00" in message:
            raise BridgeError("invalid_delivery_message")
        return {
            "deliveryAdapter": "local",
            "durationMs": 0.0,
            "t3": None,
        }


DELIVERY_ADAPTERS: dict[str, DeliveryAdapter] = {
    "t3": T3DeliveryAdapter(),
    "local": LocalDeliveryAdapter(),
}


def _launchagent_dir() -> Path:
    override = (bridge_env("LAUNCHAGENT_DIR") or "").strip()
    if override:
        return Path(override)
    home_dir = os.environ.get("HOME") or HOST_HOME
    return Path(home_dir) / "Library/LaunchAgents"


def _extract_root_from_plist(pdata: dict[str, Any]) -> tuple[Path | None, Path | None]:
    env = pdata.get("EnvironmentVariables")
    prog = pdata.get("ProgramArguments")
    root_val: str | None = None
    script_path: Path | None = None
    if isinstance(env, dict):
        override = bridge_env("ROOT", environ=env)
        if isinstance(override, str):
            root_val = override.strip()
    if isinstance(prog, list):
        for i, tok in enumerate(prog):
            if not isinstance(tok, str):
                continue
            if tok == "--root" and i + 1 < len(prog) and isinstance(prog[i + 1], str):
                root_val = prog[i + 1].strip()
            if Path(tok).name in ("gemini_task_bridge.py", "t3_gemini_bridge.py"):
                script_path = Path(tok)
    if not root_val:
        return None, script_path
    try:
        return Path(os.path.expanduser(root_val)).resolve(), script_path
    except OSError:
        return None, script_path


def _find_launchagents_for_root(root_resolved: Path) -> list[dict[str, Any]]:
    la_dir = _launchagent_dir()
    if not la_dir.is_dir():
        return []
    matches: list[dict[str, Any]] = []
    for p in sorted(la_dir.glob("*.plist")):
        if p.is_symlink():
            continue
        try:
            pdata = plistlib.loads(read_bytes(p, 65536))
        except Exception:
            continue
        if not isinstance(pdata, dict):
            continue
        plist_root, script_path = _extract_root_from_plist(pdata)
        if plist_root is None or plist_root != root_resolved:
            continue
        lbl = pdata.get("Label")
        if isinstance(lbl, str) and LABEL_RE.fullmatch(lbl):
            matches.append({"label": lbl, "plistPath": p, "scriptPath": script_path})
    return matches


def resolve_default_label(root: Path) -> str:
    override = bridge_env("LABEL")
    if override:
        return override
    matches = _find_launchagents_for_root(root.resolve())
    return matches[0]["label"] if matches else DEFAULT_LABEL


def _read_file_prefix(path: Path, max_bytes: int = 65536) -> bytes:
    """Safely read up to max_bytes from a regular single-link non-symlink file."""
    no_symlink(path)
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BridgeError("non_regular_or_hardlinked_file")
        chunks: list[bytes] = []
        remaining = max_bytes
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 16384))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _script_bridge_version(script_path: Path) -> tuple[int, ...] | None:
    try:
        data = _read_file_prefix(script_path, 65536).decode("utf-8", errors="ignore")
    except Exception:
        return None
    m = re.search(r'^BRIDGE_VERSION\s*=\s*"([0-9]+(?:\.[0-9]+)*)"', data, re.M)
    if not m:
        return (2, 0, 0)
    return _parse_version_tuple(m.group(1))


class Bridge:
    def __init__(
        self,
        root: Path | str | None = None,
        cli_path: str | None = None,
        t3_home: str | None = None,
        ax_driver: Any = None,
    ) -> None:
        self.root = Path(root) if root is not None else resolve_default_root()
        self.t3_home = t3_home if t3_home is not None else resolve_default_t3_home()
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
        import control_plane

        no_symlink(self.root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name in DIRS:
            p = self.root / name
            no_symlink(p)
            p.mkdir(mode=0o700, parents=True, exist_ok=True)

        control_plane.setup(self)
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
                if any(
                    any(x.is_file() or x.is_symlink() for x in (self.root / name).iterdir())
                    for name in ("sending", "sent", "sent/payloads", "failed", "dedup")
                ):
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
        val = decode_json(read_bytes(self.root / "ledger.json", LEDGER_HARD_MAX_BYTES))
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

    def _prune_terminal_dispatches(self, data: dict[str, Any]) -> None:
        for d in data.get("dispatches", {}).values():
            if isinstance(d, dict) and d.get("status") in TERMINAL_DISPATCH_STATUSES:
                for k in TRANSIENT_LEDGER_FIELDS:
                    d.pop(k, None)

    @contextlib.contextmanager
    def ledger_tx(self, enforce_intake_cap: bool = False):
        with file_lock(self.root / ".ledger.lock"):
            data = self.ledger()
            yield data
            self._prune_terminal_dispatches(data)
            encoded = encode_json_bytes(data)
            size = len(encoded)
            if size > LEDGER_WARN_BYTES:
                self.log("ledger_size_warning", bytes=size)
            if enforce_intake_cap and size > LEDGER_INTAKE_MAX_BYTES:
                raise BridgeError("ledger_intake_capacity_exceeded")
            if size > LEDGER_HARD_MAX_BYTES:
                raise BridgeError("ledger_size_limit_exceeded")
            atomic_json_bytes(self.root / "ledger.json", encoded)

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

    def write_health(
        self, probes: dict[str, Any], cfg: dict[str, Any] | None, *, busy_until: str | None = None,
    ) -> None:
        health = {
            "schemaVersion": 1,
            "bridgeVersion": BRIDGE_VERSION,
            "capabilities": list(BRIDGE_CAPABILITIES),
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
        if busy_until is not None:
            health["busyUntil"] = busy_until
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

    def check_v21_runtime_for_root(self, cfg: dict[str, Any] | None = None) -> tuple[bool, str]:
        """Verify whether this root is safe for v2.1 features (live v2.1 daemon or provably non-legacy offline root)."""
        lock_path = self.root / ".process.lock"
        no_symlink(lock_path)
        cfg_eff = cfg or default_config()
        poll_s = float(cfg_eff.get("poll_interval_seconds", 2.0))
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        locked_by_daemon = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
            except BlockingIOError:
                locked_by_daemon = True
        finally:
            os.close(fd)

        health = self.read_health()
        if locked_by_daemon:
            if not isinstance(health, dict):
                return False, "daemon_upgrade_required_for_v21_features"
            rfd = os.open(str(lock_path), os.O_RDONLY | os.O_NOFOLLOW)
            try:
                raw_pid = os.read(rfd, 64).decode("ascii", errors="ignore").strip()
            finally:
                os.close(rfd)
            if not raw_pid.isdigit():
                return False, "daemon_upgrade_required_for_v21_features"
            lock_pid = int(raw_pid)
            if health.get("pid") != lock_pid:
                return False, "daemon_upgrade_required_for_v21_features"
            caps = health.get("capabilities")
            if not isinstance(caps, list) or "local_adapter" not in caps:
                return False, "daemon_upgrade_required_for_v21_features"
            if _parse_version_tuple(str(health.get("bridgeVersion") or "")) < (2, 1, 0):
                return False, "daemon_upgrade_required_for_v21_features"
            import control_plane
            if not control_plane.health_fresh(health, max(15.0, 3.0 * poll_s)):
                return False, "daemon_upgrade_required_for_v21_features"
            return True, "live_v21_daemon"

        # Lock is NOT held: check LaunchAgent plists & any existing .daemon_health.json
        plists = _find_launchagents_for_root(self.root.resolve())
        if not plists:
            if health is None:
                return True, "offline_fresh_root"
            caps = health.get("capabilities") if isinstance(health, dict) else None
            if isinstance(caps, list) and "local_adapter" in caps:
                return True, "offline_v21_root"
            return False, "daemon_upgrade_required_for_v21_features"

        for pinfo in plists:
            ainfo = agent_info(pinfo["label"])
            if ainfo.get("loaded"):
                return False, "daemon_upgrade_required_for_v21_features"
            sp = pinfo.get("scriptPath")
            if sp is not None:
                ver = _script_bridge_version(sp)
                if ver is None or ver < (2, 1, 0):
                    return False, "daemon_upgrade_required_for_v21_features"
        for filename in ("gemini_task_bridge.py", "t3_gemini_bridge.py"):
            installed_script = self.root / filename
            if installed_script.exists():
                ver = _script_bridge_version(installed_script)
                if ver is None or ver < (2, 1, 0):
                    return False, "daemon_upgrade_required_for_v21_features"
        return True, "offline_upgraded_root"

    def require_v21_runtime_for_root(self, cfg: dict[str, Any] | None = None) -> None:
        ok, reason = self.check_v21_runtime_for_root(cfg)
        if not ok:
            raise BridgeError(reason)

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

    def _resolve_payload_path_for_task(self, task_id: str, adapter: str) -> Path:
        if adapter == "local" or is_reserved_task_id(task_id):
            return self.root / "sent/payloads" / f"{task_id}.json"
        return self.root / "sent" / f"{task_id}.json"

    def _payload_relpath_for_task(self, task_id: str, adapter: str) -> str:
        if adapter == "local" or is_reserved_task_id(task_id):
            return f"sent/payloads/{task_id}.json"
        return f"sent/{task_id}.json"

    def _max_archived_payload_bytes(self, cfg: dict[str, Any]) -> int:
        return ARCHIVED_PAYLOAD_READ_MAX_BYTES

    def _validate_payload_relpath(self, task_id: str, rel: Any, adapter: str | None = None) -> str:
        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
            raise BridgeError("archived_payload_corrupted")
        if rel is None:
            return self._payload_relpath_for_task(task_id, adapter or "t3")
        if adapter == "local" or is_reserved_task_id(task_id):
            allowed = {f"sent/payloads/{task_id}.json"}
        else:
            allowed = {f"sent/payloads/{task_id}.json", f"sent/{task_id}.json"}
        if not isinstance(rel, str) or rel not in allowed:
            raise BridgeError("archived_payload_corrupted")
        return rel

    def _verify_pair_continuity(
        self,
        pair_id: str,
        dispatch: dict[str, Any],
        pairs_data: dict[str, Any],
    ) -> tuple[bool, dict[str, Any] | None]:
        pair = pairs_data["pairs"].get(pair_id)
        if not isinstance(pair, dict) or not pair.get("enabled", True):
            return False, None
        expected_adapter = dispatch.get("adapter") or "t3"
        if pair_adapter(pair) != expected_adapter:
            return False, None
        expected_uid = dispatch.get("pairUid")
        if expected_uid is not None:
            if pair.get("pairUid") != expected_uid:
                return False, None
            return True, pair
        # Legacy task with pairUid == None: verify binding identity via geminiTitle & reply authorization (Condition #3)
        if expected_adapter != "t3":
            return False, None
        if dispatch.get("geminiTitle") and pair.get("geminiTitle") != dispatch.get("geminiTitle"):
            return False, None
        src_t3 = dispatch.get("sourceT3ThreadId")
        if isinstance(src_t3, str) and pair.get("t3ThreadId") != src_t3:
            return False, None
        reply_tid = dispatch.get("replyThreadId")
        if isinstance(reply_tid, str) and reply_tid not in _allowed_reply_targets_for_pair(pair, pairs_data):
            return False, None
        return True, pair

    def _resolve_v1_target(
        self,
        target: str,
        cfg: dict[str, Any],
        pairs_data: dict[str, Any],
    ) -> tuple[str, str | None]:
        if target in pairs_data["pairs"]:
            pair = pairs_data["pairs"][target]
            if pair_adapter(pair) != "t3":
                raise BridgeError("v1_receipt_forbidden_for_non_t3_task")
            return pair["defaultReplyThreadId"], target
        legacy_targets = cfg.get("legacy_targets")
        if isinstance(legacy_targets, dict) and target in legacy_targets:
            tid = legacy_targets[target]
            if isinstance(tid, str) and THREAD_ID_RE.fullmatch(tid):
                return tid, None
        raise BridgeError("target_not_whitelisted")

    def validate_outbox_receipt(
        self,
        data: Any,
        filename: str,
        cfg: dict[str, Any],
        pairs_data: dict[str, Any],
        ledger_data: dict[str, Any],
        allow_claimed_sending: bool = False,
    ) -> dict[str, Any]:
        """Validate outbox receipt against frozen dispatch ownership and adapter stage rules."""
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
        dispatch = ledger_data["dispatches"].get(task_id)
        if dispatch is not None:
            if dispatch.get("status") in ("completed", "timed_out", "failed", "unknown_timeout"):
                raise BridgeError("late_receipt_for_terminated_task")
            if dispatch.get("deliveryKind", "gemini_reply") != "gemini_reply":
                raise BridgeError("outbox_invalid_dispatch_stage")
            if dispatch.get("status") not in OUTBOX_RECEIPT_ALLOWED_STATUSES:
                raise BridgeError("outbox_invalid_dispatch_stage")

            expected_adapter = dispatch.get("adapter") or "t3"
            expected_pair_id = dispatch["pairId"]
            expected_pair_uid = dispatch.get("pairUid")
            expected_target = dispatch.get("replyThreadId")
            pair_cfg = pairs_data["pairs"].get(expected_pair_id) or {}

            # v2 schema: {"taskId", "pairId", "message", optional "captureChannel", optional "role", optional "pairUid"}
            if "pairId" in data:
                allowed_keys = {"taskId", "pairId", "message", "captureChannel", "role", "pairUid"}
                if not keys.issubset(allowed_keys):
                    raise BridgeError("invalid_fields")
                if data.get("pairId") != expected_pair_id:
                    raise BridgeError("outbox_pair_mismatch")
                if "pairUid" in data and data.get("pairUid") != expected_pair_uid:
                    raise BridgeError("outbox_pair_uid_mismatch")
                if expected_adapter == "local":
                    if expected_target is not None:
                        raise BridgeError("local_adapter_forbids_t3_thread")
                elif not isinstance(expected_target, str) or not THREAD_ID_RE.fullmatch(expected_target):
                    raise BridgeError("invalid_target_thread_id")
                role_label = str(data.get("role") or pair_cfg.get("label") or expected_pair_id)[:80]
                return {
                    "taskId": task_id,
                    "adapter": expected_adapter,
                    "resolvedThreadId": expected_target,
                    "roleLabel": role_label,
                    "pairId": expected_pair_id,
                    "pairUid": expected_pair_uid,
                    "targetTag": expected_pair_id,
                    "unregisteredReceipt": False,
                }

            # v1 compatibility schema: {"taskId", "role", "target", "message"}
            if keys == {"taskId", "role", "target", "message"}:
                if expected_adapter != "t3":
                    raise BridgeError("v1_receipt_forbidden_for_non_t3_task")
                role = data.get("role")
                target = data.get("target")
                if not isinstance(role, str) or not role.strip() or len(role) > 80 or any(ord(c) < 32 for c in role):
                    raise BridgeError("invalid_role")
                if not isinstance(target, str) or not target.strip():
                    raise BridgeError("invalid_target")
                resolved_v1, resolved_pair = self._resolve_v1_target(target, cfg, pairs_data)
                if resolved_pair != expected_pair_id or resolved_v1 != expected_target:
                    raise BridgeError("outbox_target_mismatch")
                return {
                    "taskId": task_id,
                    "adapter": "t3",
                    "resolvedThreadId": expected_target,
                    "roleLabel": role,
                    "pairId": expected_pair_id,
                    "pairUid": expected_pair_uid,
                    "targetTag": expected_pair_id,
                    "unregisteredReceipt": False,
                }

            raise BridgeError("invalid_fields")

        # Unregistered ad-hoc T3 receipt (must NOT overwrite any registered task)
        existing_t = ledger_data["tasks"].get(task_id)
        if existing_t is not None:
            if not (
                allow_claimed_sending
                and isinstance(existing_t, dict)
                and existing_t.get("status") == "sending"
                and existing_t.get("unregisteredReceipt") is True
            ):
                raise BridgeError("unregistered_receipt_cannot_override_registered_task")

        if "pairId" in data:
            allowed_keys = {"taskId", "pairId", "message", "captureChannel", "role", "pairUid"}
            if not keys.issubset(allowed_keys):
                raise BridgeError("invalid_fields")
            pair_id = data.get("pairId")
            if not isinstance(pair_id, str) or pair_id not in pairs_data["pairs"]:
                raise BridgeError("unknown_pair_id")
            pair = pairs_data["pairs"][pair_id]
            if pair_adapter(pair) != "t3":
                raise BridgeError("unregistered_local_receipt_forbidden")
            target_tid = pair["defaultReplyThreadId"]
            if target_tid not in _allowed_reply_targets_for_pair(pair, pairs_data):
                raise BridgeError("reply_target_not_whitelisted")
            role_label = str(data.get("role") or pair.get("label") or pair_id)[:80]
            return {
                "taskId": task_id,
                "adapter": "t3",
                "resolvedThreadId": target_tid,
                "roleLabel": role_label,
                "pairId": pair_id,
                "pairUid": None,
                "targetTag": pair_id,
                "unregisteredReceipt": True,
            }

        if keys == {"taskId", "role", "target", "message"}:
            role = data.get("role")
            target = data.get("target")
            if not isinstance(role, str) or not role.strip() or len(role) > 80 or any(ord(c) < 32 for c in role):
                raise BridgeError("invalid_role")
            if not isinstance(target, str) or not target.strip():
                raise BridgeError("invalid_target")
            target_tid, matched_pair = self._resolve_v1_target(target, cfg, pairs_data)
            return {
                "taskId": task_id,
                "adapter": "t3",
                "resolvedThreadId": target_tid,
                "roleLabel": role,
                "pairId": matched_pair,
                "pairUid": None,
                "targetTag": target,
                "unregisteredReceipt": True,
            }

        raise BridgeError("invalid_fields")

    def quarantine_invalid_outbox(self, path: Path, cfg: dict[str, Any], reason: str, task_id: str | None = None) -> None:
        if path.is_symlink():
            self.log("input_rejected", reason="symlink_input_rejected")
            return
        self.move(path, "failed")
        if task_id and TASK_ID_RE.fullmatch(task_id):
            with self.ledger_tx() as ledger:
                # Non-Mutation Invariant: NEVER overwrite/downgrade an already-registered task or dispatch
                if task_id not in ledger["tasks"] and task_id not in ledger["dispatches"]:
                    ledger["tasks"][task_id] = {"status": "failed", "updatedAt": now_iso(), "reason": reason}
        self.notify("Gemini Bridge Receipt Error", f"Invalid receipt not sent ({reason}); moved to failed/.", cfg["notifications"])
        self.log("input_rejected", task_id, reason)

    def _finalize_outbox_delivery(
        self,
        *,
        task_id: str,
        raw_data: dict[str, Any],
        meta: dict[str, Any],
        delivery_res: dict[str, Any],
        sending_path: Path | None,
    ) -> None:
        adapter = meta["adapter"]
        digest, byte_len = sha256_text(raw_data["message"])
        payload_rel = self._payload_relpath_for_task(task_id, adapter)
        payload_abs = self.root / payload_rel
        receipt_rel = f"sent/{task_id}.receipt.json"
        receipt_abs = self.root / receipt_rel

        payload_doc = {
            "schemaVersion": 2,
            "taskId": task_id,
            "pairId": meta.get("pairId"),
            "pairUid": meta.get("pairUid"),
            "adapter": adapter,
            "captureChannel": raw_data.get("captureChannel", "outbox_json"),
            "message": raw_data["message"],
            "messageBytes": byte_len,
            "messageSha256": digest,
            "archivedAt": now_iso(),
        }
        if meta.get("unregisteredReceipt"):
            payload_doc["unregisteredReceipt"] = True
        if raw_data.get("target") is not None:
            payload_doc["target"] = raw_data["target"]

        receipt = {
            "schemaVersion": 2,
            "taskId": task_id,
            "role": meta["roleLabel"],
            "target": meta["targetTag"],
            "pairId": meta.get("pairId"),
            "pairUid": meta.get("pairUid"),
            "deliveryAdapter": adapter,
            "resolvedThreadId": meta.get("resolvedThreadId"),
            "captureChannel": raw_data.get("captureChannel", "outbox_json"),
            "payloadRelPath": payload_rel,
            "receiptRelPath": receipt_rel,
            "accepted": True,
            "acceptedAt": now_iso(),
            "messageBytes": byte_len,
            "messageSha256": digest,
            "cliExitCode": 0 if adapter == "t3" else None,
            "cliDurationMs": delivery_res.get("durationMs", 0.0),
            "t3": delivery_res.get("t3"),
        }
        if meta.get("unregisteredReceipt"):
            receipt["unregisteredReceipt"] = True

        # Persist payload + receipt BEFORE committing terminal `sent` in ledger
        atomic_json(payload_abs, payload_doc)
        atomic_json(receipt_abs, receipt)
        with self.ledger_tx() as ledger:
            t_rec = ledger["tasks"].setdefault(task_id, {})
            t_rec.update(
                status="sent",
                updatedAt=now_iso(),
                adapter=adapter,
                pairId=meta.get("pairId"),
                pairUid=meta.get("pairUid"),
                resolvedThreadId=meta.get("resolvedThreadId"),
                payloadRelPath=payload_rel,
                receiptRelPath=receipt_rel,
                messageBytes=byte_len,
                messageSha256=digest,
                unregisteredReceipt=bool(meta.get("unregisteredReceipt")),
                receipt=receipt,
            )
            if task_id in ledger["dispatches"]:
                ledger["dispatches"][task_id].update(status="completed", completedAt=now_iso(), updatedAt=now_iso())
        if sending_path is not None and sending_path.exists() and not sending_path.is_symlink():
            sending_path.unlink()
            fsync_dir(sending_path.parent)
        self._archive_in_flight(task_id, "inbox/done")
        self.log("sent", task_id, adapter=adapter)

    def _archive_in_flight(self, task_id: str, dest_dir: str = "inbox/done") -> None:
        for folder in ("inbox/in_flight", "inbox"):
            src = self.root / folder / f"{task_id}.task.json"
            if src.exists() and not src.is_symlink():
                self.move(src, dest_dir)

    def _reconcile_sending(self, cfg: dict[str, Any]) -> None:
        with file_lock(self.root / ".ledger.lock"):
            ld_snap = self.ledger()
            pending = {k for k, v in ld_snap["tasks"].items() if v["status"] == "sending"}

        for path in sorted((self.root / "sending").glob("*.json")):
            if path.is_symlink() or path.name.startswith((".", "_")):
                continue
            tid = path.stem
            if not TASK_ID_RE.fullmatch(tid):
                self.move(path, "failed")
                continue
            with file_lock(self.root / ".ledger.lock"):
                ld_snap = self.ledger()
            prev = ld_snap["tasks"].get(tid)
            dispatch = ld_snap["dispatches"].get(tid)

            # Cutpoint (b): already committed `sent` in ledger before unlink(sending)
            if isinstance(prev, dict) and prev.get("status") == "sent":
                receipt_abs = self.root / "sent" / f"{tid}.receipt.json"
                ledger_rec = prev.get("receipt") if isinstance(prev.get("receipt"), dict) else None
                expected_sha = prev.get("messageSha256") or (ledger_rec.get("messageSha256") if ledger_rec else None)
                expected_bytes = prev.get("messageBytes") or (ledger_rec.get("messageBytes") if ledger_rec else None)
                expected_adapter = prev.get("adapter") or (ledger_rec.get("deliveryAdapter") if ledger_rec else None)
                try:
                    valid_rel = self._validate_payload_relpath(tid, prev.get("payloadRelPath"), expected_adapter)
                    if ledger_rec is not None and "payloadRelPath" in ledger_rec:
                        rec_rel = self._validate_payload_relpath(tid, ledger_rec.get("payloadRelPath"), expected_adapter)
                        if prev.get("payloadRelPath") is not None and rec_rel != valid_rel:
                            raise BridgeError("archived_payload_corrupted")
                except BridgeError:
                    self.log("recovery_blocked_noncanonical_payload_path", tid)
                    pending.discard(tid)
                    continue

                payload_ok = False
                try:
                    self.read_task_payload(tid)
                    payload_ok = True
                except (BridgeError, OSError):
                    payload_ok = False

                def _receipt_matches(rec_obj: Any) -> bool:
                    if not isinstance(rec_obj, dict):
                        return False
                    if rec_obj.get("taskId") != tid or rec_obj.get("accepted") is not True:
                        return False
                    if isinstance(expected_sha, str) and rec_obj.get("messageSha256") != expected_sha:
                        return False
                    if isinstance(expected_bytes, int) and rec_obj.get("messageBytes") != expected_bytes:
                        return False
                    if isinstance(expected_adapter, str) and rec_obj.get("deliveryAdapter", "t3") != expected_adapter:
                        return False
                    if ledger_rec is not None:
                        if rec_obj.get("acceptedAt") != ledger_rec.get("acceptedAt"):
                            return False
                        if rec_obj.get("t3") != ledger_rec.get("t3"):
                            return False
                    return True

                ledger_rec_valid = bool(
                    ledger_rec is not None
                    and ledger_rec.get("taskId") == tid
                    and ledger_rec.get("accepted") is True
                    and (not isinstance(expected_sha, str) or ledger_rec.get("messageSha256") == expected_sha)
                    and (not isinstance(expected_bytes, int) or ledger_rec.get("messageBytes") == expected_bytes)
                )

                receipt_ok = False
                if receipt_abs.exists() and not receipt_abs.is_symlink():
                    try:
                        rec_check = decode_json(read_bytes(receipt_abs, cfg["max_receipt_file_bytes"]))
                        receipt_ok = _receipt_matches(rec_check)
                    except (BridgeError, OSError):
                        receipt_ok = False

                if payload_ok and not receipt_ok and ledger_rec_valid:
                    assert ledger_rec is not None
                    atomic_json(receipt_abs, ledger_rec)
                    receipt_ok = True

                if payload_ok and receipt_ok:
                    path.unlink()
                    fsync_dir(path.parent)
                    self._archive_in_flight(tid, "inbox/done")
                    pending.discard(tid)
                    continue

                # If archived payload/receipt missing or corrupted, re-hydrate from intact sending/<tid>.json copy
                try:
                    raw_obj = decode_json(read_bytes(path, cfg["max_receipt_file_bytes"]))
                    msg = raw_obj.get("message") if isinstance(raw_obj, dict) else None
                    if isinstance(msg, str) and (not isinstance(expected_sha, str) or sha256_text(msg)[0] == expected_sha):
                        digest, byte_len = sha256_text(msg)
                        adapter_eff = expected_adapter or "t3"
                        if ledger_rec_valid:
                            assert ledger_rec is not None
                            payload_doc: dict[str, Any] = {
                                "schemaVersion": 2,
                                "taskId": tid,
                                "pairId": prev.get("pairId"),
                                "pairUid": prev.get("pairUid"),
                                "adapter": adapter_eff,
                                "captureChannel": raw_obj.get("captureChannel", prev.get("captureChannel", "outbox_json")),
                                "message": msg,
                                "messageBytes": byte_len,
                                "messageSha256": digest,
                                "archivedAt": ledger_rec.get("acceptedAt") or prev.get("updatedAt") or now_iso(),
                            }
                            if prev.get("unregisteredReceipt"):
                                payload_doc["unregisteredReceipt"] = True
                            if raw_obj.get("target") is not None:
                                payload_doc["target"] = raw_obj["target"]
                            atomic_json(self.root / valid_rel, payload_doc)
                            atomic_json(receipt_abs, ledger_rec)
                            path.unlink()
                            fsync_dir(path.parent)
                            self._archive_in_flight(tid, "inbox/done")
                            pending.discard(tid)
                            continue
                        meta = {
                            "taskId": tid,
                            "adapter": adapter_eff,
                            "resolvedThreadId": prev.get("resolvedThreadId"),
                            "roleLabel": str((ledger_rec or {}).get("role") or prev.get("pairId") or tid),
                            "pairId": prev.get("pairId"),
                            "pairUid": prev.get("pairUid"),
                            "targetTag": str((ledger_rec or {}).get("target") or prev.get("pairId") or ""),
                            "unregisteredReceipt": bool(prev.get("unregisteredReceipt")),
                        }
                        self._finalize_outbox_delivery(
                            task_id=tid,
                            raw_data=raw_obj,
                            meta=meta,
                            delivery_res={"durationMs": 0.0, "t3": (ledger_rec or {}).get("t3")},
                            sending_path=path,
                        )
                        pending.discard(tid)
                        continue
                except Exception:
                    pass
                self.move(path, "failed")
                pending.discard(tid)
                continue

            # Cutpoint (a): outbox -> sending moved, crashed BEFORE tasks[tid] pre-claim written
            if prev is None:
                try:
                    raw_bytes = read_bytes(path, cfg["max_receipt_file_bytes"])
                    raw_obj = decode_json(raw_bytes)
                    pairs_data = self.pairs()
                    meta = self.validate_outbox_receipt(raw_obj, path.name, cfg, pairs_data, ld_snap)
                except (BridgeError, OSError):
                    self.move(path, "failed")
                    if dispatch is not None and dispatch.get("status") not in TERMINAL_DISPATCH_STATUSES:
                        self.record_task_failure(tid, "failed", "recovered_interrupted_before_send", cfg)
                    continue
                if meta["adapter"] == "local":
                    self._finalize_outbox_delivery(
                        task_id=tid,
                        raw_data=raw_obj,
                        meta=meta,
                        delivery_res={"durationMs": 0.0, "t3": None},
                        sending_path=path,
                    )
                    self.log("local_recovered", tid, "pre_claim_completed")
                else:
                    # Frozen adapter is t3 (never reached deliver_to_t3_cli) -> fail without resending
                    self.move(path, "failed")
                    self.record_task_failure(tid, "failed", "recovered_interrupted_before_send", cfg)
                    self._archive_in_flight(tid, "failed")
                continue

            adapter = prev.get("adapter") or (dispatch.get("adapter") if isinstance(dispatch, dict) else None) or "t3"
            # Cutpoint (c): t3 receipt already persisted & verified on disk before ledger commit
            receipt_abs = self.root / "sent" / f"{tid}.receipt.json"
            if adapter == "t3" and receipt_abs.exists() and not receipt_abs.is_symlink():
                try:
                    rec_obj = decode_json(read_bytes(receipt_abs, cfg["max_receipt_file_bytes"]))
                    raw_obj = decode_json(read_bytes(path, cfg["max_receipt_file_bytes"]))
                    msg = raw_obj.get("message") if isinstance(raw_obj, dict) else None
                    if (
                        isinstance(rec_obj, dict)
                        and rec_obj.get("accepted") is True
                        and rec_obj.get("taskId") == tid
                        and rec_obj.get("deliveryAdapter", "t3") == "t3"
                        and isinstance(rec_obj.get("t3"), dict)
                        and isinstance(msg, str)
                        and sha256_text(msg)[0] == prev.get("messageSha256")
                        and rec_obj.get("messageSha256") == prev.get("messageSha256")
                    ):
                        meta = {
                            "taskId": tid,
                            "adapter": "t3",
                            "resolvedThreadId": prev.get("resolvedThreadId"),
                            "roleLabel": str(rec_obj.get("role") or prev.get("pairId") or tid),
                            "pairId": prev.get("pairId"),
                            "pairUid": prev.get("pairUid"),
                            "targetTag": str(rec_obj.get("target") or prev.get("pairId") or ""),
                            "unregisteredReceipt": bool(prev.get("unregisteredReceipt")),
                        }
                        self._finalize_outbox_delivery(
                            task_id=tid,
                            raw_data=raw_obj,
                            meta=meta,
                            delivery_res={"durationMs": rec_obj.get("cliDurationMs", 0.0), "t3": rec_obj.get("t3")},
                            sending_path=path,
                        )
                        pending.discard(tid)
                        self.log("t3_recovered_from_persisted_receipt", tid)
                        continue
                except Exception:
                    pass

            # Cutpoint (d): local interrupted in sending
            if adapter == "local":
                try:
                    payload_rel = self._validate_payload_relpath(tid, prev.get("payloadRelPath"), "local")
                except BridgeError:
                    self.log("recovery_blocked_noncanonical_payload_path", tid)
                    pending.discard(tid)
                    continue
                payload_abs = self.root / payload_rel
                recovered = False
                try:
                    if payload_abs.exists() and not payload_abs.is_symlink():
                        p_obj = decode_json(read_bytes(payload_abs, self._max_archived_payload_bytes(cfg)))
                        if (
                            isinstance(p_obj, dict)
                            and p_obj.get("taskId") == tid
                            and p_obj.get("pairId") == prev.get("pairId")
                            and sha256_text(str(p_obj.get("message") or ""))[0] == prev.get("messageSha256")
                        ):
                            raw_obj = {
                                "taskId": tid,
                                "pairId": p_obj.get("pairId"),
                                "message": p_obj["message"],
                                "captureChannel": p_obj.get("captureChannel", prev.get("captureChannel", "outbox_json")),
                            }
                            recovered = True
                    if not recovered:
                        raw_obj = decode_json(read_bytes(path, cfg["max_receipt_file_bytes"]))
                        if (
                            isinstance(raw_obj, dict)
                            and raw_obj.get("taskId") == tid
                            and raw_obj.get("pairId") == prev.get("pairId")
                            and sha256_text(str(raw_obj.get("message") or ""))[0] == prev.get("messageSha256")
                        ):
                            recovered = True
                    if recovered:
                        pairs_data = self.pairs()
                        p_entry = pairs_data["pairs"].get(prev.get("pairId") or "") or {}
                        meta = {
                            "taskId": tid,
                            "adapter": "local",
                            "resolvedThreadId": None,
                            "roleLabel": str(p_entry.get("label") or prev.get("pairId") or tid)[:80],
                            "pairId": prev.get("pairId"),
                            "pairUid": prev.get("pairUid"),
                            "targetTag": str(prev.get("pairId") or ""),
                            "unregisteredReceipt": False,
                        }
                        self._finalize_outbox_delivery(
                            task_id=tid,
                            raw_data=raw_obj,
                            meta=meta,
                            delivery_res={"durationMs": 0.0, "t3": None},
                            sending_path=path,
                        )
                        pending.discard(tid)
                        self.log("local_recovered", tid)
                        continue
                except Exception:
                    pass
                self.move(path, "failed")
                self.record_task_failure(tid, "failed", "local_recovery_payload_mismatch", cfg)
                self._archive_in_flight(tid, "failed")
                pending.discard(tid)
                continue

            # Cutpoint (e): t3 interrupted in sending without verified receipt -> unknown_timeout (zero retry)
            self.move(path, "failed")
            pending.add(tid)

        for tid in sorted(pending):
            with file_lock(self.root / ".ledger.lock"):
                t_rec = self.ledger()["tasks"].get(tid, {})
            if t_rec.get("status") != "sending":
                continue
            adapter = t_rec.get("adapter") or "t3"
            if adapter == "local":
                try:
                    payload_rel = self._validate_payload_relpath(tid, t_rec.get("payloadRelPath"), "local")
                except BridgeError:
                    self.log("recovery_blocked_noncanonical_payload_path", tid)
                    continue
                payload_abs = self.root / payload_rel
                if payload_abs.exists() and not payload_abs.is_symlink():
                    try:
                        p_obj = decode_json(read_bytes(payload_abs, self._max_archived_payload_bytes(cfg)))
                        if (
                            isinstance(p_obj, dict)
                            and p_obj.get("taskId") == tid
                            and sha256_text(str(p_obj.get("message") or ""))[0] == t_rec.get("messageSha256")
                        ):
                            meta = {
                                "taskId": tid,
                                "adapter": "local",
                                "resolvedThreadId": None,
                                "roleLabel": str(t_rec.get("pairId") or tid)[:80],
                                "pairId": t_rec.get("pairId"),
                                "pairUid": t_rec.get("pairUid"),
                                "targetTag": str(t_rec.get("pairId") or ""),
                                "unregisteredReceipt": False,
                            }
                            self._finalize_outbox_delivery(
                                task_id=tid,
                                raw_data={"taskId": tid, "pairId": t_rec.get("pairId"), "message": p_obj["message"], "captureChannel": p_obj.get("captureChannel", "outbox_json")},
                                meta=meta,
                                delivery_res={"durationMs": 0.0, "t3": None},
                                sending_path=None,
                            )
                            continue
                    except Exception:
                        pass
                self.record_task_failure(tid, "failed", "local_recovery_payload_mismatch", cfg)
                self._archive_in_flight(tid, "failed")
            else:
                self.record_task_failure(tid, "unknown_timeout", "recovered_interrupted_send_no_retry", cfg)
                self._archive_in_flight(tid, "failed")

    def reconcile_direct_t3_one(self, task_id: str, cfg: dict[str, Any] | None = None) -> None:
        cfg_eff = cfg or self.config()
        lock_path = self.root / "locks" / f"direct_t3.{task_id}.lock"
        with self.ledger_tx() as ld:
            d = ld["dispatches"].get(task_id)
            if not isinstance(d, dict) or d.get("status") != "direct_t3_sending":
                return
            if lock_path.exists() and not lock_path.is_symlink():
                fd = os.open(str(lock_path), os.O_RDWR | os.O_NOFOLLOW)
                try:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return  # Live CLI/MCP caller still active
                    d["status"] = "unknown_timeout"
                    d["reason"] = "direct_t3_caller_exited_unknown"
                    d["updatedAt"] = now_iso()
                    if lock_path.exists():
                        lock_path.unlink()
                    self.log("unknown_timeout", task_id, "direct_t3_caller_exited_unknown")
                finally:
                    os.close(fd)
            else:
                # Since sender unlinks lock INSIDE ledger_tx(), missing lock while still direct_t3_sending means caller exited
                d["status"] = "unknown_timeout"
                d["reason"] = "direct_t3_caller_exited_unknown"
                d["updatedAt"] = now_iso()
                self.log("unknown_timeout", task_id, "direct_t3_caller_exited_unknown")

    def reconcile_direct_t3(self, cfg: dict[str, Any]) -> None:
        with file_lock(self.root / ".ledger.lock"):
            ld = self.ledger()
            candidates = [
                tid for tid, d in ld["dispatches"].items()
                if isinstance(d, dict) and d.get("status") == "direct_t3_sending"
            ]
        for tid in candidates:
            self.reconcile_direct_t3_one(tid, cfg)

    def reconcile_staged_intake(self, cfg: dict[str, Any]) -> None:
        """Continuously publish committed reservations and sweep only dead, unreferenced staging files."""
        inbox_dir = self.root / "inbox"
        with file_lock(self.root / ".ledger.lock"):
            ld_snap = self.ledger()
            unpublished = [
                (tid, dict(d))
                for tid, d in ld_snap["dispatches"].items()
                if isinstance(d, dict) and d.get("status") == "queued" and d.get("published") is False
            ]

        def _mark_missing_if_truly_absent(tid: str, stage_path: Path | None, inbox_path: Path) -> None:
            in_fl_path = self.root / "inbox/in_flight" / f"{tid}.task.json"
            done_path = self.root / "inbox/done" / f"{tid}.task.json"
            with self.ledger_tx(enforce_intake_cap=False) as ld:
                cur = ld["dispatches"].get(tid)
                if not isinstance(cur, dict) or cur.get("status") != "queued" or cur.get("published") is not False:
                    return
                if inbox_path.exists() or in_fl_path.exists() or done_path.exists() or tid in ld["tasks"]:
                    cur["published"] = True
                    cur["updatedAt"] = now_iso()
                    return
                if stage_path is not None and stage_path.exists():
                    return
                cur["status"] = "failed"
                cur["reason"] = "intake_staging_missing"
                cur["updatedAt"] = now_iso()

        for tid, d in unpublished:
            inbox_path = inbox_dir / f"{tid}.task.json"
            in_fl_path = self.root / "inbox/in_flight" / f"{tid}.task.json"
            if inbox_path.exists() or in_fl_path.exists():
                with self.ledger_tx(enforce_intake_cap=False) as ld:
                    cur = ld["dispatches"].get(tid)
                    if isinstance(cur, dict) and cur.get("status") == "queued" and cur.get("published") is False:
                        cur["published"] = True
                        cur["updatedAt"] = now_iso()
                continue
            stage_name = d.get("stagingFile")
            if not isinstance(stage_name, str) or not stage_name.startswith("._stage."):
                _mark_missing_if_truly_absent(tid, None, inbox_path)
                continue
            stage_path = inbox_dir / stage_name
            if stage_path.is_symlink():
                with self.ledger_tx(enforce_intake_cap=False) as ld:
                    cur = ld["dispatches"].get(tid)
                    if isinstance(cur, dict) and cur.get("status") == "queued" and cur.get("published") is False:
                        cur["status"] = "failed"
                        cur["reason"] = "intake_staging_corrupted"
                        cur["updatedAt"] = now_iso()
                continue
            try:
                fd = os.open(str(stage_path), os.O_RDWR | os.O_NOFOLLOW)
            except FileNotFoundError:
                _mark_missing_if_truly_absent(tid, stage_path, inbox_path)
                continue
            except OSError:
                _mark_missing_if_truly_absent(tid, stage_path, inbox_path)
                continue
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue  # Live producer is still between Step 2 and Step 3
                try:
                    st_fd = os.fstat(fd)
                    st_path = os.lstat(str(stage_path))
                except FileNotFoundError:
                    _mark_missing_if_truly_absent(tid, stage_path, inbox_path)
                    continue
                if (st_fd.st_dev, st_fd.st_ino) != (st_path.st_dev, st_path.st_ino):
                    _mark_missing_if_truly_absent(tid, stage_path, inbox_path)
                    continue
                if inbox_path.exists() or in_fl_path.exists():
                    with self.ledger_tx(enforce_intake_cap=False) as ld:
                        cur = ld["dispatches"].get(tid)
                        if isinstance(cur, dict) and cur.get("status") == "queued":
                            cur["published"] = True
                            cur["updatedAt"] = now_iso()
                    continue
                try:
                    env = decode_json(read_bytes(stage_path, cfg["max_inbox_bytes"]))
                except (BridgeError, OSError):
                    env = None
                if (
                    not isinstance(env, dict)
                    or env.get("taskId") != tid
                    or sha256_text(str(env.get("message") or ""))[0] != d.get("requestSha256")
                ):
                    self.move(stage_path, "failed")
                    with self.ledger_tx(enforce_intake_cap=False) as ld:
                        cur = ld["dispatches"].get(tid)
                        if isinstance(cur, dict) and cur.get("status") == "queued":
                            cur["status"] = "failed"
                            cur["reason"] = "intake_staging_corrupted"
                            cur["updatedAt"] = now_iso()
                    continue
                published_here = False
                with self.ledger_tx(enforce_intake_cap=False) as ld:
                    cur = ld["dispatches"].get(tid)
                    if isinstance(cur, dict) and cur.get("status") == "queued" and cur.get("published") is False:
                        if not inbox_path.exists() and not in_fl_path.exists():
                            try:
                                atomic_rename_excl(stage_path, inbox_path, lock_dir=self.root)
                                published_here = True
                            except FileExistsError:
                                pass
                        cur["published"] = True
                        cur["updatedAt"] = now_iso()
                if published_here:
                    self.log("intake_reconciled", tid)
            finally:
                os.close(fd)

        # Sweep orphan staging files that are unreferenced AND unlocked
        for sp in sorted(inbox_dir.glob("._stage.*.task.json")):
            if sp.is_symlink() or not sp.is_file():
                continue
            with file_lock(self.root / ".ledger.lock"):
                ld_snap = self.ledger()
                referenced = any(
                    isinstance(d, dict) and d.get("stagingFile") == sp.name
                    for d in ld_snap["dispatches"].values()
                )
            if referenced:
                continue
            try:
                fd = os.open(str(sp), os.O_RDWR | os.O_NOFOLLOW)
            except OSError:
                continue
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue  # Live producer holds lock; NEVER touch
                with file_lock(self.root / ".ledger.lock"):
                    ld_snap = self.ledger()
                    referenced2 = any(
                        isinstance(d, dict) and d.get("stagingFile") == sp.name
                        for d in ld_snap["dispatches"].values()
                    )
                    if not referenced2 and sp.exists():
                        sp.unlink()
                        fsync_dir(inbox_dir)
            finally:
                os.close(fd)

    def reconcile_dispatching(self, cfg: dict[str, Any], probes: dict[str, Any], from_recover: bool = False) -> None:
        """Resolve any tasks stuck in pre-AX `dispatching` without ever auto-resubmitting."""
        with file_lock(self.root / ".ledger.lock"):
            ld_snap = self.ledger()
            dispatching = [
                (tid, dict(d))
                for tid, d in ld_snap["dispatches"].items()
                if isinstance(d, dict) and d.get("status") == "dispatching"
            ]
        if not dispatching:
            return

        ax_ok = bool(probes.get("axTrusted") and probes.get("geminiRunning") and not probes.get("screenLocked") and probes.get("geminiPid"))
        drv = self.ax() if ax_ok else None
        pid = probes.get("geminiPid")

        for tid, d in dispatching:
            with file_lock(self.root / ".ledger.lock"):
                cur_ld = self.ledger()
                cur_d = cur_ld["dispatches"].get(tid, {})
                cur_t = cur_ld["tasks"].get(tid, {})
            if cur_d.get("status") != "dispatching":
                continue

            # 1. Already sent in tasks
            if cur_t.get("status") == "sent":
                with self.ledger_tx() as ld:
                    if ld["dispatches"].get(tid, {}).get("status") == "dispatching":
                        ld["dispatches"][tid].update(status="completed", completedAt=now_iso(), updatedAt=now_iso())
                self._archive_in_flight(tid, "inbox/done")
                continue

            # 2. Early outbox or sending file exists -> let process_outbox / recover handle it
            if (self.root / "outbox" / f"{tid}.json").exists() or (self.root / "sending" / f"{tid}.json").exists():
                inbox_src = self.root / "inbox" / f"{tid}.task.json"
                if inbox_src.exists() and not inbox_src.is_symlink():
                    self.move(inbox_src, "inbox/in_flight")
                with self.ledger_tx() as ld:
                    if ld["dispatches"].get(tid, {}).get("status") == "dispatching":
                        ld["dispatches"][tid].update(status="in_flight", updatedAt=now_iso())
                continue

            # 3. Check Gemini conversation for unique start sentinel
            gem_title = d.get("geminiTitle") or ""
            resolved = False
            if drv is not None and pid and gem_title:
                with file_lock(self.root / ".gemini_ui.lock"):
                    ok_win, _ = drv.ensure_main_window_silent(pid)
                    ok_open = False
                    poll: dict[str, Any] = {}
                    if ok_win:
                        ok_open, _ = drv.open_conversation(pid, gem_title)
                        if ok_open:
                            poll = drv.poll_conversation(pid)
                if ok_win and ok_open and poll.get("window_title") == gem_title:
                    marker = f"{SENTINEL_START_PREFIX}{tid}{SENTINEL_SUFFIX}"
                    text_blob = str(poll.get("text") or "")
                    chunks = poll.get("chunks") or []
                    if marker in text_blob or any(marker in str(c) for c in chunks):
                        inbox_src = self.root / "inbox" / f"{tid}.task.json"
                        if inbox_src.exists() and not inbox_src.is_symlink():
                            self.move(inbox_src, "inbox/in_flight")
                        with self.ledger_tx() as ld:
                            if ld["dispatches"].get(tid, {}).get("status") == "dispatching":
                                ld["dispatches"][tid].update(
                                    status="in_flight",
                                    dispatchedMono=time.monotonic(),
                                    updatedAt=now_iso(),
                                )
                                ld["dispatches"][tid].pop("dispatchUncertainReason", None)
                        self.log("dispatching_promoted_via_sentinel", tid)
                        continue
                    resolved = True  # Verified window & sentinel definitely absent

            if from_recover or resolved:
                inbox_src = self.root / "inbox" / f"{tid}.task.json"
                if inbox_src.exists() and not inbox_src.is_symlink():
                    self.move(inbox_src, "failed")
                in_fl_src = self.root / "inbox/in_flight" / f"{tid}.task.json"
                if in_fl_src.exists() and not in_fl_src.is_symlink():
                    self.move(in_fl_src, "failed")
                with self.ledger_tx() as ld:
                    if ld["dispatches"].get(tid, {}).get("status") == "dispatching":
                        ld["dispatches"][tid].update(
                            status="unknown_timeout",
                            reason="dispatch_submit_outcome_unknown",
                            updatedAt=now_iso(),
                        )
                self.notify("Gemini Task Dispatch Unknown", f"Task {tid} dispatch state is uncertain; manual check needed.", cfg["notifications"])
                self.log("unknown_timeout", tid, "dispatch_submit_outcome_unknown")

    def recover(self, cfg: dict[str, Any], probes: dict[str, Any] | None = None) -> None:
        self.reconcile_staged_intake(cfg)
        self._reconcile_sending(cfg)
        self.reconcile_direct_t3(cfg)
        self.reconcile_dispatching(cfg, probes if probes is not None else self.system_probes(), from_recover=True)

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
            meta = self.validate_outbox_receipt(data, path.name, cfg, pairs_data, ledger_data)
            task_id = meta["taskId"]
        except (BridgeError, OSError) as exc:
            with file_lock(self.root / ".ledger.lock"):
                self.ledger()
            self.quarantine_invalid_outbox(
                path, cfg, str(exc) if isinstance(exc, BridgeError) else "input_io_error", task_id
            )
            return

        claimed = self.move(path, "sending")
        digest, byte_len = sha256_text(data["message"])
        adapter_name = meta["adapter"]
        payload_rel = self._payload_relpath_for_task(task_id, adapter_name)

        with self.ledger_tx() as ledger:
            ledger["tasks"][task_id] = {
                "status": "sending",
                "updatedAt": now_iso(),
                "startedAt": now_iso(),
                "adapter": adapter_name,
                "pairId": meta.get("pairId"),
                "pairUid": meta.get("pairUid"),
                "target": meta["targetTag"],
                "resolvedThreadId": meta.get("resolvedThreadId"),
                "payloadRelPath": payload_rel,
                "captureChannel": data.get("captureChannel", "outbox_json"),
                "unregisteredReceipt": bool(meta.get("unregisteredReceipt")),
                "messageSha256": digest,
                "messageBytes": byte_len,
                "attempts": 0,
            }
        self.log("sending", task_id, adapter=adapter_name)

        try:
            latest_cfg = self.config()
            latest_pairs = self.pairs()
            if read_bytes(claimed, latest_cfg["max_receipt_file_bytes"]) != raw:
                raise BridgeError("input_changed_before_claim")
            with file_lock(self.root / ".ledger.lock"):
                latest_ledger = self.ledger()
            re_meta = self.validate_outbox_receipt(
                data, claimed.name, latest_cfg, latest_pairs, latest_ledger, allow_claimed_sending=True
            )
            if (
                re_meta["resolvedThreadId"] != meta["resolvedThreadId"]
                or re_meta["adapter"] != adapter_name
                or re_meta["pairUid"] != meta["pairUid"]
            ):
                raise BridgeError("target_changed_before_send")
        except (BridgeError, OSError) as exc:
            self.move(claimed, "failed")
            self.record_task_failure(
                task_id, "failed", str(exc) if isinstance(exc, BridgeError) else "preflight_io_error", cfg
            )
            self._archive_in_flight(task_id, "failed")
            return

        with self.ledger_tx() as ledger:
            ledger["tasks"][task_id]["attempts"] = 1
            ledger["tasks"][task_id]["cliInvokedAt"] = now_iso()

        adapter = DELIVERY_ADAPTERS[adapter_name]
        try:
            delivery_res = adapter.deliver(self, task_id, meta.get("resolvedThreadId"), data["message"])
        except BridgeError as exc:
            reason = str(exc)
            status = "unknown_timeout" if reason == "cli_timeout_no_retry" else "failed"
            self.record_task_failure(task_id, status, reason, cfg)
            self.move(claimed, "failed")
            self._archive_in_flight(task_id, "failed")
            return

        self._finalize_outbox_delivery(
            task_id=task_id,
            raw_data=data,
            meta=meta,
            delivery_res=delivery_res,
            sending_path=claimed,
        )

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
                env = decode_json(read_bytes(path, IN_FLIGHT_ENVELOPE_READ_MAX_BYTES))
                if (
                    isinstance(env, dict)
                    and isinstance(env.get("pairId"), str)
                    and isinstance(env.get("taskId"), str)
                    and path.name == f"{env['taskId']}.task.json"
                ):
                    result[env["pairId"]] = env["taskId"]
            except Exception:
                continue
        try:
            with file_lock(self.root / ".ledger.lock"):
                ld = self.ledger()
            # A frozen dispatch is authoritative, including terminal states.
            # Discard envelope claims before rebuilding from the ledger so a
            # damaged pairId cannot temporarily occupy an unrelated pair.
            result = {pair: tid for pair, tid in result.items() if tid not in ld["dispatches"]}
            for tid, d in ld["dispatches"].items():
                if (
                    isinstance(d, dict)
                    and d.get("status") in ("dispatching", "in_flight", "needs_attention", "captured_to_outbox")
                    and isinstance(d.get("pairId"), str)
                ):
                    result.setdefault(d["pairId"], tid)
        except Exception:
            pass
        return result

    def _enter_needs_attention(self, tid: str, gem_title: str, d_rec: dict[str, Any], cfg: dict[str, Any], card_mode: bool = False) -> None:
        with self.ledger_tx() as ld:
            cur = ld["dispatches"].setdefault(tid, {})
            already_notified = bool(cur.get("notifiedAttention"))
            ep = int(cur.get("attentionEpisode") or 0)
            if not already_notified:
                ep += 1
            cur.update(
                status="needs_attention",
                notifiedAttention=True,
                attentionEpisode=ep,
                idleSinceMono=None,
                stableSinceMono=None,
                stableCount=0,
                lastPollLen=None,
                updatedAt=now_iso(),
            )
        if not d_rec.get("notifiedAttention"):
            msg = (
                f"Conversation '{gem_title}' is showing a confirmation card; please confirm in Gemini."
                if card_mode
                else f"Conversation '{gem_title}' is waiting for manual confirmation in Gemini."
            )
            self.notify("Gemini Task Needs Attention", msg, cfg["notifications"])

    def _clear_needs_attention(self, tid: str) -> None:
        with self.ledger_tx() as ld:
            cur = ld["dispatches"].setdefault(tid, {})
            cur.update(
                status="in_flight",
                notifiedAttention=False,
                idleSinceMono=None,
                stableSinceMono=None,
                stableCount=0,
                lastPollLen=None,
                updatedAt=now_iso(),
            )

    def reconcile_legacy_in_flight(self, cfg: dict[str, Any]) -> None:
        """Persist complete dispatch metadata for legacy v2.0 in_flight envelopes before outbox or timeout handling."""
        in_flight_files = sorted((self.root / "inbox/in_flight").glob("*.task.json"))
        if not in_flight_files:
            return
        now_mono = time.monotonic()
        with file_lock(self.root / ".pairs.lock"):
            pairs_data = self.pairs()
            with file_lock(self.root / ".ledger.lock"):
                ledger_snap = self.ledger()

        for path in in_flight_files:
            if path.name.startswith((".", "_")) or path.is_symlink():
                continue
            cand_tid = path.name[:-len(".task.json")] if path.name.endswith(".task.json") else None
            if isinstance(cand_tid, str) and TASK_ID_RE.fullmatch(cand_tid):
                t_pre = ledger_snap["tasks"].get(cand_tid)
                if isinstance(t_pre, dict) and t_pre.get("status") == "sent":
                    continue
                d_pre = ledger_snap["dispatches"].get(cand_tid)
                if isinstance(d_pre, dict) and "status" in d_pre and "pairId" in d_pre:
                    continue
            try:
                in_flight_limit = max(cfg["max_inbox_bytes"], IN_FLIGHT_ENVELOPE_READ_MAX_BYTES)
                task = decode_json(read_bytes(path, in_flight_limit))
            except Exception:
                self.move(path, "failed")
                continue
            if not isinstance(task, dict):
                self.move(path, "failed")
                continue
            tid = task.get("taskId")
            if not isinstance(tid, str) or not TASK_ID_RE.fullmatch(tid) or path.name != f"{tid}.task.json":
                self.move(path, "failed")
                continue
            t_rec = ledger_snap["tasks"].get(tid)
            if isinstance(t_rec, dict) and t_rec.get("status") == "sent":
                continue
            existing = ledger_snap["dispatches"].get(tid)
            if isinstance(existing, dict) and "status" in existing and "pairId" in existing:
                continue

            pid_name = task.get("pairId")
            pair = pairs_data["pairs"].get(pid_name) if isinstance(pid_name, str) else None
            if not pair or not pair.get("enabled", True) or pair_adapter(pair) != "t3":
                self.move(path, "failed")
                self.log("legacy_in_flight_rejected", tid, "unknown_disabled_or_non_t3_pair")
                continue
            reply_tid = task.get("replyThreadId") or pair.get("defaultReplyThreadId")
            if (
                not isinstance(reply_tid, str)
                or not THREAD_ID_RE.fullmatch(reply_tid)
                or reply_tid not in _allowed_reply_targets_for_pair(pair, pairs_data)
            ):
                self.move(path, "failed")
                self.log("legacy_in_flight_rejected", tid, "unauthorized_reply_target")
                continue

            prev_mono = existing.get("dispatchedMono") if isinstance(existing, dict) else None
            mono_eff = prev_mono if isinstance(prev_mono, (int, float)) and prev_mono <= now_mono else now_mono
            with self.ledger_tx(enforce_intake_cap=False) as ld:
                cur_d = ld["dispatches"].get(tid)
                if not isinstance(cur_d, dict) or "status" not in cur_d or "pairId" not in cur_d:
                    ld["dispatches"][tid] = {
                        "status": "in_flight",
                        "deliveryKind": "gemini_reply",
                        "pairId": pid_name,
                        "pairUid": None,
                        "adapter": "t3",
                        "sourceT3ThreadId": pair["t3ThreadId"],
                        "geminiTitle": pair["geminiTitle"],
                        "replyThreadId": reply_tid,
                        "replyMode": task.get("replyMode") or pair.get("replyMode", "auto"),
                        "dispatchedMono": mono_eff,
                        "createdAt": task.get("createdAt") or now_iso(),
                        "updatedAt": now_iso(),
                        "baselineTextLen": 0,
                        "baselineChunks": [],
                        "baselineUnknown": True,
                    }
                    self.log("legacy_in_flight_hydrated", tid, pairId=pid_name)

    def process_in_flight(self, cfg: dict[str, Any], probes: dict[str, Any]) -> None:
        self.reconcile_legacy_in_flight(cfg)
        in_flight_files = sorted((self.root / "inbox/in_flight").glob("*.task.json"))
        with file_lock(self.root / ".ledger.lock"):
            ledger_snap = self.ledger()
        has_active_dispatches = any(
            isinstance(d, dict) and d.get("status") in ("in_flight", "needs_attention", "captured_to_outbox")
            for d in ledger_snap["dispatches"].values()
        )
        if not in_flight_files and not has_active_dispatches:
            return
        pairs_data = self.pairs()
        now_mono = time.monotonic()
        in_flight_limit = max(cfg["max_inbox_bytes"], IN_FLIGHT_ENVELOPE_READ_MAX_BYTES)

        remaining: list[tuple[Path | None, dict[str, Any]]] = []
        seen_tids: set[str] = set()
        for path in in_flight_files:
            if path.name.startswith((".", "_")) or path.is_symlink():
                continue
            file_tid = path.name[:-len(".task.json")] if path.name.endswith(".task.json") else None
            if not isinstance(file_tid, str) or not TASK_ID_RE.fullmatch(file_tid):
                self.move(path, "failed")
                continue
            try:
                task = decode_json(read_bytes(path, in_flight_limit))
            except Exception:
                self.move(path, "failed")
                continue
            if not isinstance(task, dict):
                self.move(path, "failed")
                continue
            body_tid = task.get("taskId")
            body_pair = task.get("pairId")
            d_check = ledger_snap["dispatches"].get(file_tid)
            if (
                not isinstance(body_tid, str)
                or body_tid != file_tid
                or not isinstance(body_pair, str)
                or (
                    isinstance(d_check, dict)
                    and isinstance(d_check.get("pairId"), str)
                    and body_pair != d_check.get("pairId")
                )
            ):
                self.move(path, "failed")
                self.log("in_flight_file_rejected", file_tid, "filename_body_or_pair_mismatch")
                continue
            tid = file_tid
            seen_tids.add(tid)
            t_rec = ledger_snap["tasks"].get(tid)
            # First clean up any task already marked sent in ledger
            if t_rec and t_rec.get("status") == "sent":
                self.move(path, "inbox/done")
                with self.ledger_tx() as ld:
                    if tid in ld["dispatches"]:
                        ld["dispatches"][tid].update(status="completed", updatedAt=now_iso())
                continue
            d_rec = ledger_snap["dispatches"].get(tid, {})
            if d_rec.get("status") in TERMINAL_DISPATCH_STATUSES:
                dest = "inbox/done" if d_rec.get("status") == "completed" else "failed"
                self.move(path, dest)
                continue
            if (self.root / "outbox" / f"{tid}.json").exists():
                # Pending outbox exists (not terminal yet; let process_outbox validate it)
                continue
            remaining.append((path, task))

        for tid, d_rec in ledger_snap["dispatches"].items():
            if tid in seen_tids or not isinstance(d_rec, dict):
                continue
            if d_rec.get("status") not in ("in_flight", "needs_attention", "captured_to_outbox"):
                continue
            t_rec = ledger_snap["tasks"].get(tid)
            if t_rec and t_rec.get("status") == "sent":
                with self.ledger_tx() as ld:
                    if tid in ld["dispatches"]:
                        ld["dispatches"][tid].update(status="completed", updatedAt=now_iso())
                continue
            if (self.root / "outbox" / f"{tid}.json").exists():
                continue
            remaining.append((
                None,
                {
                    "taskId": tid,
                    "pairId": d_rec.get("pairId", ""),
                    "replyThreadId": d_rec.get("replyThreadId"),
                    "replyMode": d_rec.get("replyMode"),
                },
            ))

        if not remaining:
            return

        # Timeout check doesn't require GUI access
        still_active: list[tuple[Path | None, dict[str, Any]]] = []
        for path, task in remaining:
            tid = task["taskId"]
            d_rec = ledger_snap["dispatches"].get(tid, {})
            disp_mono = d_rec.get("dispatchedMono")
            # Also handle daemon restarts where dispatchedMono wasn't from current boot
            if not isinstance(disp_mono, (int, float)) or disp_mono > now_mono:
                with self.ledger_tx() as ld:
                    cur_d = ld["dispatches"].get(tid)
                    if isinstance(cur_d, dict) and "status" in cur_d and "pairId" in cur_d:
                        cur_d["dispatchedMono"] = now_mono
                disp_mono = now_mono
            if (now_mono - disp_mono) > cfg["max_task_wait_seconds"]:
                reason = (
                    "max_task_wait_exceeded_while_needs_attention"
                    if d_rec.get("status") == "needs_attention"
                    else "max_task_wait_exceeded"
                )
                if path is not None and path.exists() and not path.is_symlink():
                    self.move(path, "failed")
                self.notify("Gemini Task Timeout", f"Task {tid} exceeded {cfg['max_task_wait_seconds']}s without completion.", cfg["notifications"])
                with self.ledger_tx() as ld:
                    ld["dispatches"].setdefault(tid, {}).update(
                        status="timed_out", updatedAt=now_iso(), reason=reason
                    )
                self.log("in_flight_timed_out", tid, reason)
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
            with file_lock(self.root / ".pairs.lock"):
                pairs_data = self.pairs()
                with file_lock(self.root / ".ledger.lock"):
                    d_rec = dict(self.ledger()["dispatches"].get(tid, {}))
            if not d_rec:
                # Legacy v2.0 in_flight envelope without ledger dispatch snapshot
                pair = pairs_data["pairs"].get(pid_name)
                if not pair or not pair.get("enabled", True) or pair_adapter(pair) != "t3":
                    if path is not None and path.exists() and not path.is_symlink():
                        self.move(path, "failed")
                    continue
                d_rec = {
                    "pairId": pid_name,
                    "pairUid": None,
                    "adapter": "t3",
                    "geminiTitle": pair["geminiTitle"],
                    "replyThreadId": task.get("replyThreadId") or pair["defaultReplyThreadId"],
                    "status": "in_flight",
                }
            else:
                ok_cont, pair = self._verify_pair_continuity(pid_name, d_rec, pairs_data)
                if not ok_cont or pair is None:
                    if path is not None and path.exists() and not path.is_symlink():
                        self.move(path, "failed")
                    with self.ledger_tx() as ld:
                        ld["dispatches"].setdefault(tid, {}).update(
                            status="failed", reason="pair_rebound_or_removed", updatedAt=now_iso()
                        )
                    self.log("in_flight_aborted", tid, "pair_rebound_or_removed", pairId=pid_name)
                    continue

            gem_title = d_rec.get("geminiTitle") or pair["geminiTitle"]
            reply_mode = task.get("replyMode") or pair.get("replyMode", "auto")
            adapter_name = d_rec.get("adapter") or pair_adapter(pair)
            badge = badge_by_title.get(gem_title)
            in_attention = d_rec.get("status") == "needs_attention"

            if badge == "needs_attention":
                self._enter_needs_attention(tid, gem_title, d_rec, cfg, card_mode=False)
                continue

            # When already in_attention (regardless of badge/reply_mode), verify via window poll before clearing.
            # Also check window when badge == "in_progress" so in-window confirmation_card always takes precedence over in_progress.
            if in_attention or badge == "in_progress":
                with file_lock(self.root / ".gemini_ui.lock"):
                    ok_open, open_reason = drv.open_conversation(pid, gem_title)
                    poll = drv.poll_conversation(pid) if ok_open else {}
                if not ok_open or poll.get("window_title") != gem_title:
                    # Unreliable observation must NEVER clear needs_attention
                    continue
                if poll.get("confirmation_card"):
                    self._enter_needs_attention(tid, gem_title, d_rec, cfg, card_mode=True)
                    continue
                if in_attention:
                    self._clear_needs_attention(tid)
                    in_attention = False
                if badge == "in_progress" or poll.get("busy"):
                    if d_rec.get("idleSinceMono") is not None or d_rec.get("stableCount"):
                        with self.ledger_tx() as ld:
                            ld["dispatches"].setdefault(tid, {}).update(
                                status="in_flight", idleSinceMono=None, stableSinceMono=None, stableCount=0, updatedAt=now_iso()
                            )
                    continue
                # If badge != "in_progress" and we exited attention to idle, fall through to grace/capture (or outbox card monitor)
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
                self._enter_needs_attention(tid, gem_title, d_rec, cfg, card_mode=True)
                continue
            if poll.get("busy"):
                with self.ledger_tx() as ld:
                    ld["dispatches"].setdefault(tid, {}).update(
                        status="in_flight", idleSinceMono=None, stableSinceMono=None, stableCount=0, updatedAt=now_iso()
                    )
                continue

            # For reply_mode == "outbox", polling above was solely for confirmation card detection/clearing
            if reply_mode == "outbox":
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
            if d_rec.get("baselineUnknown"):
                start_marker = f"{SENTINEL_START_PREFIX}{tid}{SENTINEL_SUFFIX}"
                poll_text_str = str(poll.get("text") or "")
                poll_chunks_list = [c for c in (poll.get("chunks") or []) if isinstance(c, str)]
                has_start_boundary = (start_marker in poll_text_str) or any(start_marker in c for c in poll_chunks_list)
                if not (has_start_boundary and has_sentinel):
                    continue
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
                    if adapter_name == "local":
                        msg = reply_text
                    else:
                        msg = f"[Bridge Reply: {label} -> T3] Task: {tid}\n{reply_text}"
                    msg = _clip_to_limits(msg, cfg["max_message_bytes"], cfg["max_message_lines"])
                    receipt_payload: dict[str, Any] = {
                        "taskId": tid,
                        "pairId": pid_name,
                        "captureChannel": "ax_capture_sentinel" if has_sentinel else "ax_capture_stable",
                        "message": msg,
                    }
                    if d_rec.get("pairUid") is not None:
                        receipt_payload["pairUid"] = d_rec["pairUid"]
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

    def _reject_inbox_task(self, path: Path, tid: str | None, reason: str, *, from_filename: bool = False) -> None:
        if tid is None and path.name.endswith(".task.json"):
            cand = path.name[:-len(".task.json")]
            if TASK_ID_RE.fullmatch(cand):
                tid = cand
                from_filename = True
        self.move(path, "failed")
        if tid and TASK_ID_RE.fullmatch(tid):
            with self.ledger_tx(enforce_intake_cap=False) as ld:
                existing = ld["dispatches"].get(tid)
                if from_filename:
                    if isinstance(existing, dict) and existing.get("status") == "queued":
                        existing.update(status="failed", reason=reason, updatedAt=now_iso())
                elif existing is None or (isinstance(existing, dict) and existing.get("status") == "queued"):
                    rec = ld["dispatches"].setdefault(tid, {})
                    rec.update(status="failed", reason=reason, updatedAt=now_iso())
        self.log("inbox_task_rejected", tid, reason)

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
            file_tid = path.name[:-len(".task.json")] if path.name.endswith(".task.json") else None
            if not isinstance(file_tid, str) or not TASK_ID_RE.fullmatch(file_tid):
                file_tid = None
            try:
                task = decode_json(read_bytes(path, cfg["max_inbox_bytes"]))
                if not isinstance(task, dict):
                    raise BridgeError("invalid_inbox_task")
                body_tid = task.get("taskId")
                pid_name = task.get("pairId")
                msg = task.get("message")
                reply_tid = task.get("replyThreadId")
                if (
                    file_tid is None
                    or not isinstance(body_tid, str)
                    or body_tid != file_tid
                    or not isinstance(pid_name, str)
                    or not isinstance(msg, str)
                    or not msg.strip()
                ):
                    raise BridgeError("invalid_inbox_task")
                tid = file_tid
            except (BridgeError, OSError) as exc:
                self._reject_inbox_task(
                    path,
                    file_tid,
                    str(exc) if isinstance(exc, BridgeError) else "io_error",
                    from_filename=True,
                )
                continue

            with file_lock(self.root / ".pairs.lock"):
                pairs_data = self.pairs()
                with file_lock(self.root / ".ledger.lock"):
                    ld_snap = self.ledger()
                    d_rec = ld_snap["dispatches"].get(tid)

            pair = pairs_data["pairs"].get(pid_name)
            if not pair or not pair.get("enabled", True):
                self._reject_inbox_task(path, tid, "unknown_or_disabled_pair")
                continue

            adapter_name = pair_adapter(pair)
            if adapter_name == "t3":
                if not isinstance(reply_tid, str) or not THREAD_ID_RE.fullmatch(reply_tid):
                    self._reject_inbox_task(path, tid, "invalid_inbox_task")
                    continue
            elif reply_tid is not None:
                self._reject_inbox_task(path, tid, "local_adapter_forbids_t3_thread")
                continue

            # Consumer ownership check when ledger dispatch reservation exists
            if d_rec is not None:
                if d_rec.get("status") != "queued":
                    # Never re-submit tasks already in dispatching / in_flight / terminal state
                    continue
                req_sha = sha256_text(msg)[0]
                if (
                    d_rec.get("pairId") != pid_name
                    or d_rec.get("adapter", "t3") != adapter_name
                    or d_rec.get("pairUid") != pair.get("pairUid")
                    or d_rec.get("geminiTitle") != pair["geminiTitle"]
                    or d_rec.get("replyThreadId") != reply_tid
                    or (d_rec.get("requestSha256") is not None and d_rec.get("requestSha256") != req_sha)
                ):
                    self._reject_inbox_task(path, tid, "intake_ownership_mismatch")
                    continue
            else:
                # Legacy v2.0 compat envelope without dispatch snapshot
                if adapter_name != "t3" or reply_tid not in _allowed_reply_targets_for_pair(pair, pairs_data):
                    self._reject_inbox_task(path, tid, "legacy_envelope_unauthorized")
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

            gem_title = pair["geminiTitle"]

            # Step A: probe window & capture bounded baseline under .gemini_ui.lock
            with file_lock(self.root / ".gemini_ui.lock"):
                ok_win, _ = drv.ensure_main_window_silent(pid)
                if not ok_win:
                    self._record_cooldown(tid, "no_main_window", cfg)
                    continue
                ok_open, open_reason = drv.open_conversation(pid, gem_title)
                if not ok_open:
                    self._record_cooldown(tid, open_reason, cfg)
                    continue
                poll_a = drv.poll_conversation(pid)
                if poll_a.get("window_title") != gem_title:
                    self._record_cooldown(tid, "title_verification_failed", cfg)
                    continue
                if poll_a.get("busy") or poll_a.get("confirmation_card"):
                    continue
                base_chunks = bound_baseline_chunks(poll_a.get("chunks") or [])
                base_text_len = len(str(poll_a.get("text") or ""))

            # Step B: persist transitional `dispatching` intent & verify 28 MiB intake cap BEFORE AX submit
            had_prior_dispatch = d_rec is not None
            prior_dispatch_copy = dict(d_rec) if d_rec is not None else None
            try:
                with self.ledger_tx(enforce_intake_cap=True) as ld:
                    cur_d = ld["dispatches"].get(tid)
                    if cur_d is not None and cur_d.get("status") != "queued":
                        raise BridgeError("dispatch_state_raced")
                    rec = ld["dispatches"].setdefault(tid, {
                        "pairId": pid_name,
                        "pairUid": None,  # Legacy unreserved v2.0 envelope must never inherit a newer pairUid
                        "adapter": adapter_name,
                        "deliveryKind": "gemini_reply",
                        "sourceT3ThreadId": pair.get("t3ThreadId") if adapter_name == "t3" else None,
                        "geminiTitle": gem_title,
                        "replyThreadId": reply_tid,
                        "requestSha256": sha256_text(msg)[0],
                        "createdAt": task.get("createdAt") or now_iso(),
                    })
                    rec.update(
                        status="dispatching",
                        replyMode=task.get("replyMode") or pair.get("replyMode", "auto"),
                        dispatchAttemptAt=now_iso(),
                        dispatchAttemptMono=time.monotonic(),
                        updatedAt=now_iso(),
                        dispatchedMono=time.monotonic(),
                        baselineTextLen=base_text_len,
                        baselineChunks=base_chunks,
                        idleSinceMono=None,
                        stableSinceMono=None,
                        stableCount=0,
                        lastPollLen=None,
                        notifiedAttention=False,
                    )
            except BridgeError as exc:
                self.log("dispatch_blocked", tid, str(exc), pairId=pid_name)
                continue

            # Step C (Condition #1): re-acquire .gemini_ui.lock, re-open & verify frozen geminiTitle/busy/card right before submit!
            reverted_before_submit = False
            sub_reason = ""
            ok_sub = False
            poll_c: dict[str, Any] = {}
            with file_lock(self.root / ".gemini_ui.lock"):
                ok_win2, _ = drv.ensure_main_window_silent(pid)
                if not ok_win2:
                    reverted_before_submit = True
                    sub_reason = "no_main_window"
                else:
                    ok_open2, open_reason2 = drv.open_conversation(pid, gem_title)
                    if not ok_open2:
                        reverted_before_submit = True
                        sub_reason = open_reason2
                    else:
                        poll_c = drv.poll_conversation(pid)
                        if poll_c.get("window_title") != gem_title:
                            reverted_before_submit = True
                            sub_reason = "title_verification_failed"
                        elif poll_c.get("busy") or poll_c.get("confirmation_card"):
                            reverted_before_submit = True
                            sub_reason = "window_busy_or_confirmation_card"
                        else:
                            framed = build_framed_prompt(
                                task_id=tid,
                                pair_id=pid_name,
                                reply_thread_id=reply_tid,
                                message=msg,
                                outbox_path=self.root / "outbox" / f"{tid}.json",
                                adapter=adapter_name,
                            )
                            ok_sub, sub_reason = drv.write_prompt_and_submit(pid, framed)

            if reverted_before_submit or (not ok_sub and sub_reason in PRE_SUBMIT_REVERT_REASONS):
                with self.ledger_tx(enforce_intake_cap=False) as ld:
                    cur = ld["dispatches"].get(tid)
                    if isinstance(cur, dict) and cur.get("status") == "dispatching":
                        if had_prior_dispatch and prior_dispatch_copy is not None:
                            ld["dispatches"][tid] = prior_dispatch_copy
                            ld["dispatches"][tid]["updatedAt"] = now_iso()
                        else:
                            ld["dispatches"].pop(tid, None)
                if sub_reason != "window_busy_or_confirmation_card":
                    self._record_cooldown(tid, sub_reason, cfg)
                continue

            if not ok_sub:
                # Uncertain submit (`submit_press_uncertain` / `submit_unconfirmed`): keep in `dispatching`, zero re-press!
                with self.ledger_tx(enforce_intake_cap=False) as ld:
                    cur = ld["dispatches"].get(tid)
                    if isinstance(cur, dict) and cur.get("status") == "dispatching":
                        cur["dispatchUncertainReason"] = sub_reason
                        cur["updatedAt"] = now_iso()
                active_by_pair[pid_name] = tid
                self.log("dispatch_uncertain", tid, sub_reason, pairId=pid_name)
                continue

            self._dispatch_cooldown.pop(tid, None)
            if path.exists() and not path.is_symlink():
                self.move(path, "inbox/in_flight")
            active_by_pair[pid_name] = tid
            with self.ledger_tx(enforce_intake_cap=False) as ld:
                cur = ld["dispatches"].get(tid)
                if isinstance(cur, dict) and cur.get("status") == "dispatching":
                    cur.update(
                        status="in_flight",
                        dispatchedAt=now_iso(),
                        updatedAt=now_iso(),
                        dispatchedMono=time.monotonic(),
                        baselineTextLen=len(str(poll_c.get("text") or "")),
                        baselineChunks=bound_baseline_chunks(poll_c.get("chunks") or []),
                    )
                    cur.pop("dispatchAttemptAt", None)
                    cur.pop("dispatchAttemptMono", None)
                    cur.pop("dispatchUncertainReason", None)
                elif isinstance(cur, dict) and cur.get("status") == "completed":
                    in_fl = self.root / "inbox/in_flight" / f"{tid}.task.json"
                    if in_fl.exists() and not in_fl.is_symlink():
                        self.move(in_fl, "inbox/done")
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
        import control_plane

        cfg = self.config()
        self.pairs()
        with file_lock(self.root / ".ledger.lock"):
            self.ledger()
        probes = self.system_probes()
        self.write_health(probes, cfg)
        if recover:
            self.recover(cfg, probes)
        else:
            self.reconcile_staged_intake(cfg)
            self.reconcile_direct_t3(cfg)
        self.reconcile_legacy_in_flight(cfg)
        self.process_outbox(cfg)
        self.process_in_flight(cfg, probes)
        self.process_outbox(cfg)
        control_plane.process_control(self, cfg, probes, recover=recover)
        self.process_inbox(cfg, probes)
        self.reconcile_dispatching(cfg, probes, from_recover=False)
        self.process_outbox(cfg)
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

    def _task_exists_in_queues(self, task_id: str) -> bool:
        candidates = (
            self.root / "inbox" / f"{task_id}.task.json",
            self.root / "inbox/in_flight" / f"{task_id}.task.json",
            self.root / "outbox" / f"{task_id}.json",
            self.root / "sending" / f"{task_id}.json",
            self.root / "sent/payloads" / f"{task_id}.json",
            self.root / "sent" / f"{task_id}.receipt.json",
            self.root / "sent" / f"{task_id}.json",
        )
        return any(p.exists() or p.is_symlink() for p in candidates)

    def _has_unregistered_queue_tasks_for_pair(self, pair_id: str, ld_snap: dict[str, Any]) -> bool:
        """Return True if any queued/in_flight envelope for pair_id lacks a ledger snapshot or holds pairUid=None."""
        dispatches = ld_snap.get("dispatches") or {}
        for folder in ("inbox", "inbox/in_flight"):
            for p in (self.root / folder).glob("*.task.json"):
                if p.name.startswith((".", "_")) or p.is_symlink():
                    continue
                try:
                    env = decode_json(read_bytes(p, 524288))
                except Exception:
                    continue
                if not isinstance(env, dict) or env.get("pairId") != pair_id:
                    continue
                etid = env.get("taskId")
                if not isinstance(etid, str):
                    continue
                d_rec = dispatches.get(etid)
                if d_rec is None:
                    return True
                if (
                    isinstance(d_rec, dict)
                    and d_rec.get("pairUid") is None
                    and d_rec.get("status") in ACTIVE_DISPATCH_STATUSES
                ):
                    return True
        return False

    def submit_task(
        self,
        *,
        pair_id: str,
        message: str,
        task_id: str | None = None,
        reply_to: str | None = None,
        idempotency_key: str | None = None,
        forbid_legacy_pair_uid_deferral: bool = False,
        allowed_adapters: set[str] | frozenset[str] | None = None,
        allow_pairs: set[str] | frozenset[str] | None = None,
        owner_scope: str | None = None,
        expected_pair_uid: Any = _UNSET,
        expected_adapter: str | None = None,
        expected_gemini_title: str | None = None,
        expected_t3_thread_id: Any = _UNSET,
        expected_default_reply_thread_id: Any = _UNSET,
        require_v21_runtime: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(pair_id, str) or not PAIR_ID_RE.fullmatch(pair_id):
            raise BridgeError("invalid_pair_id")
        if not message or not message.strip() or "\x00" in message:
            raise BridgeError("invalid_message")

        if task_id is not None:
            if not TASK_ID_RE.fullmatch(task_id):
                raise BridgeError("invalid_task_id")
            if is_reserved_task_id(task_id):
                raise BridgeError("reserved_task_id_suffix")
        if idempotency_key is not None and not IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
            raise BridgeError("invalid_idempotency_key")

        req_sha256, _ = sha256_text(message)

        stage_path: Path | None = None
        stage_fd: int | None = None
        direct_lock_fd: int | None = None
        direct_lock_path: Path | None = None
        tid = task_id or make_task_id()
        envelope: dict[str, Any] = {}
        resolved_reply_to: str | None = None
        adapter_name = "t3"
        effective = "gemini"
        pair_uid: str | None = None
        pair_copy: dict[str, Any] = {}
        replay_tid: str | None = None
        v21_ok = False

        try:
            with file_lock(self.root / ".pairs.lock"):
                cfg = self.config()
                if len(message.encode("utf-8")) > cfg["max_message_bytes"] or len(message.splitlines()) > cfg["max_message_lines"]:
                    raise BridgeError("message_limit_exceeded")
                v21_ok, gate_reason = self.check_v21_runtime_for_root(cfg)
                if require_v21_runtime and not v21_ok:
                    raise BridgeError(gate_reason)

                pairs_data = self.pairs()
                scoped = (allowed_adapters is not None or allow_pairs is not None or owner_scope is not None)
                if pair_id not in pairs_data["pairs"]:
                    raise BridgeError("pair_not_accessible_in_scope" if scoped else "unknown_pair_id")
                pair = pairs_data["pairs"][pair_id]
                if not pair_scope_allowed(pair_id, pair, owner_scope, allow_pairs):
                    raise BridgeError("pair_not_accessible_in_scope")
                if not pair.get("enabled", True):
                    raise BridgeError("pair_not_accessible_in_scope" if scoped else "pair_disabled")
                adapter_name = pair_adapter(pair)
                if allowed_adapters is not None and adapter_name not in allowed_adapters:
                    raise BridgeError("pair_not_accessible_in_scope")
                if expected_adapter is not None and adapter_name != expected_adapter:
                    raise BridgeError("pair_not_accessible_in_scope")
                if expected_pair_uid is not _UNSET and pair.get("pairUid") != expected_pair_uid:
                    raise BridgeError("pair_not_accessible_in_scope")
                if expected_gemini_title is not None and pair.get("geminiTitle") != expected_gemini_title:
                    raise BridgeError("pair_not_accessible_in_scope")
                if expected_t3_thread_id is not _UNSET and pair.get("t3ThreadId") != expected_t3_thread_id:
                    raise BridgeError("pair_not_accessible_in_scope")
                if expected_default_reply_thread_id is not _UNSET and pair.get("defaultReplyThreadId") != expected_default_reply_thread_id:
                    raise BridgeError("pair_not_accessible_in_scope")

                effective = resolve_effective_route(cfg, pair)
                if effective == "paused":
                    raise BridgeError("gemini_routing_paused_by_global_mode")

                if adapter_name == "local":
                    if not v21_ok:
                        raise BridgeError(gate_reason)
                    if reply_to is not None:
                        raise BridgeError("local_pair_does_not_support_reply_to")
                    resolved_reply_to = None
                else:
                    resolved_reply_to = reply_to or pair["defaultReplyThreadId"]
                    if not isinstance(resolved_reply_to, str) or not THREAD_ID_RE.fullmatch(resolved_reply_to):
                        raise BridgeError("invalid_reply_thread_id")
                    if resolved_reply_to not in _allowed_reply_targets_for_pair(pair, pairs_data):
                        raise BridgeError("reply_target_not_whitelisted")

                if idempotency_key is not None and not v21_ok:
                    raise BridgeError(gate_reason)

                pair_uid = pair.get("pairUid")
                pair_copy = dict(pair)

                if v21_ok and pair_uid is None:
                    with file_lock(self.root / ".ledger.lock"):
                        ld_chk = self.ledger()
                        has_active_legacy = any(
                            isinstance(d, dict)
                            and d.get("pairId") == pair_id
                            and d.get("pairUid") is None
                            and d.get("status") in ACTIVE_DISPATCH_STATUSES
                            for d in ld_chk["dispatches"].values()
                        ) or self._has_unregistered_queue_tasks_for_pair(pair_id, ld_chk)
                    if has_active_legacy:
                        if forbid_legacy_pair_uid_deferral:
                            raise BridgeError("pair_has_active_legacy_task_retry_after_drain")
                    else:
                        pair_uid = uuid.uuid4().hex
                        pairs_data["pairs"][pair_id]["pairUid"] = pair_uid
                        pairs_data["pairs"][pair_id]["updatedAt"] = now_iso()
                        validate_pairs_data(pairs_data)
                        atomic_json(self.root / "pairs.json", pairs_data)
                        pair_copy["pairUid"] = pair_uid

                # Direct T3 route
                if effective == "t3":
                    if not v21_ok:
                        if idempotency_key is not None:
                            raise BridgeError(gate_reason)
                        res = self.deliver_to_t3_cli(pair_copy["t3ThreadId"], message)
                        with self.ledger_tx(enforce_intake_cap=False) as ld:
                            ld["dispatches"][tid] = {
                                "status": "direct_t3",
                                "pairId": pair_id,
                                "t3ThreadId": pair_copy["t3ThreadId"],
                                "createdAt": now_iso(),
                                "updatedAt": now_iso(),
                            }
                        self.log("sent_direct_t3", tid, pairId=pair_id)
                        return {"ok": True, "routed": "t3", "taskId": tid, "pairId": pair_id, "threadId": pair_copy["t3ThreadId"], **res}

                    with self.ledger_tx(enforce_intake_cap=True) as ld:
                        if idempotency_key is not None:
                            idem_map = ld.setdefault("idempotencyKeys", {})
                            scope_k = f"{pair_id}:{pair_uid or 'legacy'}:{idempotency_key}"
                            if scope_k in idem_map:
                                prev = idem_map[scope_k]
                                if prev.get("requestSha256") != req_sha256:
                                    raise BridgeError("idempotency_key_conflict")
                                replay_tid = prev["taskId"]
                        if replay_tid is None:
                            if tid in ld["tasks"] or tid in ld["dispatches"] or self._task_exists_in_queues(tid):
                                raise BridgeError("duplicate_task_id")
                            direct_lock_path = self.root / "locks" / f"direct_t3.{tid}.lock"
                            no_symlink(direct_lock_path)
                            direct_lock_fd = os.open(str(direct_lock_path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                            fcntl.flock(direct_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            ld["dispatches"][tid] = {
                                "status": "direct_t3_sending",
                                "deliveryKind": "direct_t3_forwarded",
                                "pairId": pair_id,
                                "pairUid": pair_uid,
                                "adapter": "t3",
                                "sourceT3ThreadId": pair_copy["t3ThreadId"],
                                "t3ThreadId": pair_copy["t3ThreadId"],
                                "requestSha256": req_sha256,
                                "createdAt": now_iso(),
                                "updatedAt": now_iso(),
                            }
                            if idempotency_key is not None:
                                ld["idempotencyKeys"][scope_k] = {
                                    "taskId": tid,
                                    "requestSha256": req_sha256,
                                    "createdAt": now_iso(),
                                }
                    if replay_tid is not None:
                        return {"ok": True, "idempotentReplay": True, "routed": "t3", "taskId": replay_tid, "pairId": pair_id, "pairUid": pair_uid}
                else:
                    # Gemini route
                    envelope = {
                        "schemaVersion": 2,
                        "taskId": tid,
                        "pairId": pair_id,
                        "replyThreadId": resolved_reply_to,
                        "replyMode": pair_copy.get("replyMode", "auto"),
                        "message": message,
                        "createdAt": now_iso(),
                    }
                    envelope_bytes = encode_json_bytes(envelope)
                    if len(envelope_bytes) > cfg["max_inbox_bytes"]:
                        raise BridgeError("inbox_envelope_limit_exceeded")
                    if not v21_ok:
                        # Legacy v2.0 Compat Write Path (no v2.1 dispatches pre-entry; single-link atomic publish)
                        with file_lock(self.root / ".ledger.lock"):
                            ld_snap = self.ledger()
                            if tid in ld_snap["tasks"] or tid in ld_snap["dispatches"] or self._task_exists_in_queues(tid):
                                raise BridgeError("duplicate_task_id")
                        stage_path = self.root / "inbox" / f"._stage.{tid}.{uuid.uuid4().hex[:8]}.task.json"
                        no_symlink(stage_path)
                        stage_fd = os.open(str(stage_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                        fcntl.flock(stage_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        os.write(stage_fd, envelope_bytes)
                        os.fsync(stage_fd)
                        fsync_dir(self.root / "inbox")
                    else:
                        # v2.1 3-Phase Atomic Intake (Stage 1 -> Stage 2 under .ledger.lock)
                        stage_path = self.root / "inbox" / f"._stage.{tid}.{uuid.uuid4().hex[:8]}.task.json"
                        no_symlink(stage_path)
                        stage_fd = os.open(str(stage_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                        fcntl.flock(stage_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        os.write(stage_fd, envelope_bytes)
                        os.fsync(stage_fd)
                        fsync_dir(self.root / "inbox")

                        with self.ledger_tx(enforce_intake_cap=True) as ld:
                            if idempotency_key is not None:
                                idem_map = ld.setdefault("idempotencyKeys", {})
                                scope_k = f"{pair_id}:{pair_uid or 'legacy'}:{idempotency_key}"
                                if scope_k in idem_map:
                                    prev = idem_map[scope_k]
                                    if prev.get("requestSha256") != req_sha256:
                                        raise BridgeError("idempotency_key_conflict")
                                    replay_tid = prev["taskId"]
                            if replay_tid is None:
                                if tid in ld["tasks"] or tid in ld["dispatches"] or self._task_exists_in_queues(tid):
                                    raise BridgeError("duplicate_task_id")
                                ld["dispatches"][tid] = {
                                    "status": "queued",
                                    "deliveryKind": "gemini_reply",
                                    "pairId": pair_id,
                                    "pairUid": pair_uid,
                                    "adapter": adapter_name,
                                    "sourceT3ThreadId": pair_copy.get("t3ThreadId") if adapter_name == "t3" else None,
                                    "geminiTitle": pair_copy["geminiTitle"],
                                    "replyThreadId": resolved_reply_to,
                                    "requestSha256": req_sha256,
                                    "stagingFile": stage_path.name,
                                    "published": False,
                                    "createdAt": envelope["createdAt"],
                                    "updatedAt": now_iso(),
                                }
                                if idempotency_key is not None:
                                    ld["idempotencyKeys"][scope_k] = {
                                        "taskId": tid,
                                        "requestSha256": req_sha256,
                                        "createdAt": now_iso(),
                                    }
                        if replay_tid is not None:
                            if stage_path.exists():
                                stage_path.unlink()
                            return {
                                "ok": True,
                                "idempotentReplay": True,
                                "routed": "gemini",
                                "taskId": replay_tid,
                                "pairId": pair_id,
                                "pairUid": pair_uid,
                                "adapter": adapter_name,
                                "geminiTitle": pair_copy["geminiTitle"],
                                "replyThreadId": resolved_reply_to,
                            }

            # Locks .pairs.lock and .ledger.lock are now released!
            if effective == "t3":
                # Invokes external CLI while still holding `direct_lock_fd`
                try:
                    res = self.deliver_to_t3_cli(pair_copy["t3ThreadId"], message)
                except BridgeError as exc:
                    reason = str(exc)
                    fail_st = "unknown_timeout" if reason == "cli_timeout_no_retry" else "failed"
                    with self.ledger_tx(enforce_intake_cap=False) as ld:
                        d = ld["dispatches"].setdefault(tid, {})
                        d.update(status=fail_st, reason=reason, updatedAt=now_iso())
                        if direct_lock_path is not None and direct_lock_path.exists():
                            direct_lock_path.unlink()
                    self.log(fail_st, tid, reason, pairId=pair_id)
                    raise
                with self.ledger_tx(enforce_intake_cap=False) as ld:
                    ld["dispatches"][tid].update(status="direct_t3", updatedAt=now_iso())
                    if direct_lock_path is not None and direct_lock_path.exists():
                        direct_lock_path.unlink()
                self.log("sent_direct_t3", tid, pairId=pair_id)
                return {
                    "ok": True,
                    "routed": "t3",
                    "taskId": tid,
                    "pairId": pair_id,
                    "threadId": pair_copy["t3ThreadId"],
                    **res,
                }

            # Stage 3: serialize final publish + published=True against reconcile_staged_intake
            inbox_path = self.root / "inbox" / f"{tid}.task.json"
            assert stage_path is not None
            if v21_ok:
                with self.ledger_tx(enforce_intake_cap=False) as ld:
                    try:
                        atomic_rename_excl(stage_path, inbox_path, lock_dir=self.root)
                    except FileExistsError:
                        raise BridgeError("duplicate_task_id") from None
                    cur = ld["dispatches"].get(tid)
                    if isinstance(cur, dict) and cur.get("status") == "queued":
                        cur["published"] = True
                        cur["updatedAt"] = now_iso()
            else:
                try:
                    atomic_rename_excl(stage_path, inbox_path, lock_dir=self.root)
                except FileExistsError:
                    raise BridgeError("duplicate_task_id") from None
            self.log("queued_for_gemini", tid, pairId=pair_id, adapter=adapter_name)
            return {
                "ok": True,
                "routed": "gemini",
                "queued": True,
                "taskId": tid,
                "pairId": pair_id,
                "pairUid": pair_uid,
                "adapter": adapter_name,
                "geminiTitle": pair_copy["geminiTitle"],
                "replyThreadId": resolved_reply_to,
                "inboxFile": str(inbox_path),
            }
        except Exception:
            if stage_path is not None and stage_path.exists():
                try:
                    stage_path.unlink()
                except OSError:
                    pass
            raise
        finally:
            if stage_fd is not None:
                os.close(stage_fd)
            if direct_lock_fd is not None:
                os.close(direct_lock_fd)

    def read_task_payload(self, task_id: str) -> dict[str, Any]:
        """Verify and read archived Gemini reply payload for a completed task."""
        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
            raise BridgeError("invalid_task_id")
        cfg = self.config()
        with file_lock(self.root / ".ledger.lock"):
            ld = self.ledger()
        t_rec = ld["tasks"].get(task_id)
        d_rec = ld["dispatches"].get(task_id)

        if d_rec and d_rec.get("status") in ("direct_t3", "direct_t3_sending"):
            raise BridgeError("direct_t3_has_no_gemini_reply")
        if not t_rec or t_rec.get("status") != "sent":
            if d_rec and d_rec.get("status") in ("failed", "timed_out", "unknown_timeout"):
                raise BridgeError(f"task_not_completed:{d_rec['status']}")
            raise BridgeError("task_not_completed")

        candidates: list[Path] = []
        rel = t_rec.get("payloadRelPath")
        if rel is not None:
            valid_rel = self._validate_payload_relpath(task_id, rel, t_rec.get("adapter"))
            candidates.append(self.root / valid_rel)
        else:
            candidates.append(self.root / "sent/payloads" / f"{task_id}.json")
            candidates.append(self.root / "sent" / f"{task_id}.json")

        target_path: Path | None = None
        for cand in candidates:
            if cand.is_symlink():
                raise BridgeError("archived_payload_corrupted")
            if cand.exists():
                target_path = cand
                break

        if target_path is None:
            raise BridgeError("historical_payload_unavailable")

        try:
            raw = read_bytes(target_path, self._max_archived_payload_bytes(cfg))
            obj = decode_json(raw)
        except (BridgeError, OSError):
            raise BridgeError("archived_payload_corrupted") from None

        if not isinstance(obj, dict) or target_path.name != f"{task_id}.json":
            raise BridgeError("archived_payload_corrupted")
        if obj.get("taskId") != task_id:
            raise BridgeError("archived_payload_corrupted")
        # Distinguish archived payload vs receipt file in flat sent/
        if "accepted" in obj and "message" not in obj:
            raise BridgeError("archived_payload_corrupted")
        msg = obj.get("message")
        if not isinstance(msg, str) or not msg.strip():
            raise BridgeError("archived_payload_corrupted")

        expected_pair = t_rec.get("pairId") or (d_rec.get("pairId") if isinstance(d_rec, dict) else None)
        if expected_pair is not None and "pairId" in obj and obj.get("pairId") != expected_pair:
            raise BridgeError("archived_payload_corrupted")
        expected_uid = t_rec.get("pairUid") or (d_rec.get("pairUid") if isinstance(d_rec, dict) else None)
        if expected_uid is not None and "pairUid" in obj and obj.get("pairUid") != expected_uid:
            raise BridgeError("archived_payload_corrupted")

        digest, byte_len = sha256_text(msg)
        expected_sha = t_rec.get("messageSha256") or (t_rec.get("receipt") or {}).get("messageSha256")
        if isinstance(expected_sha, str) and digest != expected_sha:
            raise BridgeError("archived_payload_corrupted")
        expected_bytes = t_rec.get("messageBytes") or (t_rec.get("receipt") or {}).get("messageBytes")
        if isinstance(expected_bytes, int) and byte_len != expected_bytes:
            raise BridgeError("archived_payload_corrupted")

        return {
            "ok": True,
            "taskId": task_id,
            "pairId": expected_pair,
            "pairUid": expected_uid,
            "adapter": t_rec.get("adapter") or obj.get("adapter") or "t3",
            "message": msg,
            "messageBytes": byte_len,
            "messageSha256": digest,
            "payloadFile": str(target_path),
            "receipt": t_rec.get("receipt"),
        }

    def get_task_status(self, task_id: str, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
            raise BridgeError("invalid_task_id")
        cfg_eff = cfg or self.config()
        with file_lock(self.root / ".ledger.lock"):
            ld = self.ledger()
        t_rec = ld["tasks"].get(task_id)
        d_rec = ld["dispatches"].get(task_id)

        if isinstance(d_rec, dict) and d_rec.get("status") == "direct_t3_sending":
            self.reconcile_direct_t3_one(task_id, cfg_eff)
            with file_lock(self.root / ".ledger.lock"):
                ld = self.ledger()
                t_rec = ld["tasks"].get(task_id)
                d_rec = ld["dispatches"].get(task_id)

        base: dict[str, Any] = {"ok": True, "taskId": task_id}
        if isinstance(d_rec, dict):
            base.update({
                "pairId": d_rec.get("pairId"),
                "pairUid": d_rec.get("pairUid"),
                "adapter": d_rec.get("adapter", "t3"),
                "geminiTitle": d_rec.get("geminiTitle"),
                "replyThreadId": d_rec.get("replyThreadId"),
                "attentionEpisode": d_rec.get("attentionEpisode", 0),
            })
        elif isinstance(t_rec, dict):
            base.update({
                "pairId": t_rec.get("pairId"),
                "pairUid": t_rec.get("pairUid"),
                "adapter": t_rec.get("adapter", "t3"),
                "replyThreadId": t_rec.get("resolvedThreadId"),
            })

        # Terminal states take strict precedence
        if isinstance(t_rec, dict):
            ts = t_rec.get("status")
            if ts == "sent":
                base.update({
                    "status": "sent",
                    "terminal": True,
                    "deliveryKind": "gemini_reply",
                    "hasReply": True,
                    "receipt": t_rec.get("receipt"),
                    "updatedAt": t_rec.get("updatedAt"),
                })
                return base
            if ts in ("failed", "unknown_timeout"):
                base.update({
                    "status": ts,
                    "terminal": True,
                    "reason": t_rec.get("reason"),
                    "hasReply": False,
                    "updatedAt": t_rec.get("updatedAt"),
                })
                return base

        if isinstance(d_rec, dict):
            ds = d_rec.get("status")
            if ds == "direct_t3":
                base.update({
                    "status": "direct_t3",
                    "terminal": True,
                    "deliveryKind": "direct_t3_forwarded",
                    "hasReply": False,
                    "updatedAt": d_rec.get("updatedAt"),
                })
                return base
            if ds in ("completed", "timed_out", "failed", "unknown_timeout"):
                base.update({
                    "status": ds,
                    "terminal": True,
                    "reason": d_rec.get("reason"),
                    "hasReply": ds == "completed",
                    "updatedAt": d_rec.get("updatedAt"),
                })
                return base

        # Active states
        if isinstance(t_rec, dict) and t_rec.get("status") == "sending":
            base.update({"status": "sending", "terminal": False, "hasReply": False, "updatedAt": t_rec.get("updatedAt")})
            return base
        if (self.root / "sending" / f"{task_id}.json").exists():
            base.update({"status": "sending", "terminal": False, "hasReply": False})
            return base
        if (self.root / "outbox" / f"{task_id}.json").exists() or (isinstance(d_rec, dict) and d_rec.get("status") == "captured_to_outbox"):
            base.update({"status": "captured_to_outbox", "terminal": False, "hasReply": False})
            return base
        if isinstance(d_rec, dict):
            ds = d_rec.get("status")
            if ds in ("direct_t3_sending", "needs_attention", "in_flight", "dispatching"):
                base.update({"status": ds, "terminal": False, "hasReply": False, "updatedAt": d_rec.get("updatedAt")})
                return base
            if ds == "queued":
                paused = cfg_eff.get("global_mode") == "t3_only"
                base.update({"status": "queued", "terminal": False, "hasReply": False, "pausedByGlobalMode": paused, "updatedAt": d_rec.get("updatedAt")})
                return base

        if (self.root / "inbox/in_flight" / f"{task_id}.task.json").exists():
            base.update({"status": "in_flight", "terminal": False, "hasReply": False})
            return base
        if (self.root / "inbox" / f"{task_id}.task.json").exists():
            base.update({"status": "queued", "terminal": False, "hasReply": False, "pausedByGlobalMode": cfg_eff.get("global_mode") == "t3_only"})
            return base
        if (self.root / "failed" / f"{task_id}.task.json").exists() or (self.root / "failed" / f"{task_id}.json").exists():
            base.update({"status": "failed", "terminal": True, "hasReply": False, "reason": "quarantined_in_failed"})
            return base

        base.update({"status": "not_found", "terminal": False, "hasReply": False})
        return base

    def wait_for_task(
        self,
        task_id: str,
        timeout_s: float = 300.0,
        return_on_attention: bool = False,
        cancel_event: Any = None,
        shutdown_event: Any = None,
    ) -> tuple[int, dict[str, Any]]:
        """Shared bounded wait loop used by CLI `send --wait`, `task wait`, and MCP server."""
        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
            raise BridgeError("invalid_task_id")
        deadline = time.monotonic() + max(0.5, float(timeout_s))
        missing_count = 0
        last_status: dict[str, Any] = {"status": "wait_timeout"}

        while True:
            if shutdown_event is not None and shutdown_event.is_set():
                return 2, {"ok": False, "completed": False, "taskId": task_id, "status": "shutdown", "reason": "server_shutdown"}
            if cancel_event is not None and cancel_event.is_set():
                return 2, {"ok": False, "completed": False, "taskId": task_id, "status": "cancelled", "reason": "request_cancelled"}

            st = self.get_task_status(task_id)
            last_status = st
            cur = st["status"]

            if cur == "sent":
                payload_msg: str | None = None
                payload_err: str | None = None
                try:
                    p = self.read_task_payload(task_id)
                    payload_msg = p["message"]
                except BridgeError as exc:
                    payload_err = str(exc)
                if payload_err is not None:
                    with file_lock(self.root / ".ledger.lock"):
                        t_rec_snap = dict(self.ledger()["tasks"].get(task_id) or {})
                    if payload_err == "historical_payload_unavailable" and t_rec_snap.get("payloadRelPath") is None:
                        return 0, {
                            "ok": True,
                            "completed": True,
                            "routed": "gemini",
                            "taskId": task_id,
                            "pairId": st.get("pairId"),
                            "status": "sent",
                            "message": None,
                            "receipt": st.get("receipt"),
                            "payloadStatus": payload_err,
                        }
                    return 2, {
                        "ok": False,
                        "completed": True,
                        "routed": "gemini",
                        "taskId": task_id,
                        "pairId": st.get("pairId"),
                        "status": "sent",
                        "error": payload_err,
                        "reason": payload_err,
                    }
                return 0, {
                    "ok": True,
                    "completed": True,
                    "routed": "gemini",
                    "taskId": task_id,
                    "pairId": st.get("pairId"),
                    "status": "sent",
                    "message": payload_msg,
                    "receipt": st.get("receipt"),
                }

            if cur == "direct_t3":
                return 0, {
                    "ok": True,
                    "completed": True,
                    "routed": "t3",
                    "taskId": task_id,
                    "pairId": st.get("pairId"),
                    "status": "direct_t3",
                    "deliveryKind": "direct_t3_forwarded",
                    "hasReply": False,
                    "message": None,
                }

            if cur in ("failed", "unknown_timeout", "timed_out"):
                return 2, {
                    "ok": False,
                    "completed": True,
                    "routed": "gemini",
                    "taskId": task_id,
                    "pairId": st.get("pairId"),
                    "status": cur,
                    "reason": st.get("reason"),
                }

            if return_on_attention and cur == "needs_attention":
                return 3, {
                    "ok": False,
                    "completed": False,
                    "routed": "gemini",
                    "taskId": task_id,
                    "pairId": st.get("pairId"),
                    "geminiTitle": st.get("geminiTitle"),
                    "status": "needs_attention",
                    "attentionEpisode": st.get("attentionEpisode", 0),
                    "reason": "manual_confirmation_required",
                }

            if cur == "not_found":
                missing_count += 1
                if missing_count >= 2:
                    return 2, {
                        "ok": False,
                        "completed": True,
                        "taskId": task_id,
                        "status": "not_found",
                        "reason": "task_not_found",
                    }
            else:
                missing_count = 0

            now_m = time.monotonic()
            if now_m >= deadline:
                break
            sleep_for = min(0.25, max(0.05, deadline - now_m))
            if cancel_event is not None:
                cancel_event.wait(sleep_for)
            elif shutdown_event is not None:
                shutdown_event.wait(sleep_for)
            else:
                time.sleep(sleep_for)

        return 1, {
            "ok": False,
            "completed": False,
            "routed": "gemini",
            "taskId": task_id,
            "pairId": last_status.get("pairId"),
            "status": last_status.get("status", "wait_timeout"),
            "reason": "wait_timeout",
        }


def _validated_label(label: str | None = None, *, root: Path | None = None) -> tuple[str, Path]:
    lbl = resolve_default_label(root if root is not None else resolve_default_root()) if label is None else label
    if not isinstance(lbl, str) or not LABEL_RE.fullmatch(lbl) or ".." in lbl:
        raise BridgeError("invalid_launchagent_label")
    return lbl, _launchagent_dir() / (lbl + ".plist")


def agent_info(label: str | None = None, *, root: Path | None = None) -> dict[str, Any]:
    if sys.platform != "darwin":
        return {"loaded": False, "reason": "non_macos"}
    try:
        lbl, _ = _validated_label(label, root=root)
    except BridgeError as exc:
        return {"loaded": False, "reason": str(exc)}
    try:
        res = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/{lbl}"],
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
    t3_cli_required = any(
        isinstance(p, dict) and p.get("enabled", True) and pair_adapter(p) == "t3"
        for p in pairs_data["pairs"].values()
    )
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
        "bridgeVersion": BRIDGE_VERSION,
        "python": sys.version.split()[0],
        "queueRoot": str(bridge.root),
        "globalMode": cfg["global_mode"],
        "pairCount": len(pairs_data["pairs"]),
        "currentProcess": probes,
        "geminiAppVersion": gem_ver,
        "geminiWindowTitle": win_title,
        "t3CliRequired": t3_cli_required,
        "t3CliPath": bridge.cli_path,
        "t3CliExecutable": t3_cli_ok,
        "t3ServerRuntime": t3_runtime,
        "launchAgent": agent_info(root=bridge.root),
        "daemonHealth": health,
    }
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Bridge: {BRIDGE_VERSION} | Python: {report['python']} | Root: {report['queueRoot']}")
        print(f"Global Mode: {report['globalMode']} | Pairs: {report['pairCount']}")
        print(f"Gemini: running={probes['geminiRunning']} pid={probes['geminiPid']} ver={gem_ver} win={win_title!r}")
        print(f"Current AXTrusted: {probes['axTrusted']} | ScreenLocked: {probes['screenLocked']}")
        print(f"T3 CLI: {bridge.cli_path} (required={t3_cli_required}, executable={t3_cli_ok})")
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
        "bridgeVersion": BRIDGE_VERSION,
        "agent": agent_info(root=bridge.root),
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
                "pairUid": p.get("pairUid"),
                "adapter": pair_adapter(p),
                "label": p.get("label"),
                "t3ThreadId": p.get("t3ThreadId"),
                "defaultReplyThreadId": p.get("defaultReplyThreadId"),
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
            dispatchStates={
                k: sum(v.get("status") == k for v in ledger_data["dispatches"].values())
                for k in ("queued", "dispatching", "in_flight", "needs_attention", "captured_to_outbox", "direct_t3_sending", "direct_t3", "completed", "timed_out", "failed", "unknown_timeout")
            },
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


def _resolve_pair_adapter_args(
    adapter_arg: str | None,
    t3_thread: str | None,
    reply_to: str | None,
    route: str = "gemini",
) -> tuple[str, str | None, str | None]:
    if adapter_arg is None:
        if t3_thread is None:
            raise BridgeError("t3_thread_required_for_t3_adapter")
        adapter = "t3"
    else:
        adapter = adapter_arg
        if adapter not in ("t3", "local"):
            raise BridgeError("invalid_pair_adapter")

    if adapter == "local":
        if t3_thread is not None:
            raise BridgeError("local_adapter_forbids_t3_thread")
        if reply_to is not None:
            raise BridgeError("local_adapter_forbids_reply_to")
        if route != "gemini":
            raise BridgeError("local_pair_has_no_t3_route")
        return "local", None, None

    if not isinstance(t3_thread, str) or not THREAD_ID_RE.fullmatch(t3_thread):
        raise BridgeError("invalid_t3_thread_id")
    rep = reply_to or t3_thread
    if not isinstance(rep, str) or not THREAD_ID_RE.fullmatch(rep):
        raise BridgeError("invalid_reply_thread_id")
    return "t3", t3_thread, rep


def _has_active_tasks_for_pair(bridge: Bridge, pair_id: str) -> bool:
    with file_lock(bridge.root / ".ledger.lock"):
        ld = bridge.ledger()
    for d in ld["dispatches"].values():
        if isinstance(d, dict) and d.get("pairId") == pair_id and d.get("status") in ACTIVE_DISPATCH_STATUSES:
            return True
    for p in (bridge.root / "inbox").glob("*.task.json"):
        if p.name.startswith((".", "_")) or p.is_symlink():
            continue
        try:
            env = decode_json(read_bytes(p, 524288))
            if isinstance(env, dict) and env.get("pairId") == pair_id:
                return True
        except Exception:
            continue
    for p in (bridge.root / "inbox/in_flight").glob("*.task.json"):
        if p.name.startswith((".", "_")) or p.is_symlink():
            continue
        try:
            env = decode_json(read_bytes(p, 524288))
            if isinstance(env, dict) and env.get("pairId") == pair_id:
                return True
        except Exception:
            continue
    return False


def cmd_pair(bridge: Bridge, args: argparse.Namespace) -> int:
    bridge.setup()
    cfg = bridge.config()
    if args.pair_cmd == "list":
        pdata = bridge.pairs()
        out = []
        for pid, p in sorted(pdata["pairs"].items()):
            item = dict(p)
            item["adapter"] = pair_adapter(p)
            item["effectiveRoute"] = resolve_effective_route(cfg, p)
            out.append(item)
        print(json.dumps({"globalMode": cfg["global_mode"], "pairs": out}, ensure_ascii=False, indent=2))
        return 0

    if args.pair_cmd in ("init", "gemini-chats", "request"):
        import control_plane
        return control_plane.run_cli(bridge, cfg, args)

    if args.pair_cmd == "add":
        if not PAIR_ID_RE.fullmatch(args.id):
            raise BridgeError("invalid_pair_id")
        if not args.gemini_title or not args.gemini_title.strip():
            raise BridgeError("invalid_pair_gemini_title")
        adapter, t3_thread, reply_to = _resolve_pair_adapter_args(
            getattr(args, "adapter", None),
            getattr(args, "t3_thread", None),
            getattr(args, "reply_to", None),
            route=args.route,
        )
        ws = str(Path(args.workspace).expanduser().resolve()) if args.workspace else None
        force = bool(getattr(args, "force", False))
        gem_title = args.gemini_title.strip()

        with bridge.pairs_tx() as pdata:
            if adapter == "local":
                bridge.require_v21_runtime_for_root(cfg)
            prev = pdata["pairs"].get(args.id)
            if prev is not None and not force and _has_active_tasks_for_pair(bridge, args.id):
                raise BridgeError("pair_has_active_task")

            identity_changed = (
                prev is None
                or pair_adapter(prev) != adapter
                or prev.get("geminiTitle") != gem_title
                or prev.get("t3ThreadId") != t3_thread
                or prev.get("defaultReplyThreadId") != reply_to
            )
            if identity_changed or not isinstance(prev.get("pairUid"), str):
                pair_uid = uuid.uuid4().hex
            else:
                pair_uid = prev["pairUid"]

            pdata["pairs"][args.id] = {
                "pairId": args.id,
                "pairUid": pair_uid,
                "adapter": adapter,
                "label": args.label or args.id,
                "t3ThreadId": t3_thread,
                "defaultReplyThreadId": reply_to,
                "allowedReplyThreadIds": sorted({t3_thread, reply_to}) if adapter == "t3" else [],
                "geminiTitle": gem_title,
                "workspacePath": ws,
                "route": args.route,
                "replyMode": args.reply_mode,
                "enabled": True,
                "createdAt": (prev or {}).get("createdAt") or now_iso(),
                "updatedAt": now_iso(),
            }
            if prev is not None and pair_uid == prev.get("pairUid"):
                for field in ("ownerScope", "createdVia", "controlRequestId"):
                    if field in prev:
                        pdata["pairs"][args.id][field] = prev[field]
        print(json.dumps({"ok": True, "pair": pdata["pairs"][args.id]}, ensure_ascii=False, indent=2))
        return 0

    if args.pair_cmd == "route":
        with bridge.pairs_tx() as pdata:
            if args.id not in pdata["pairs"]:
                raise BridgeError("unknown_pair_id")
            pair = pdata["pairs"][args.id]
            if pair_adapter(pair) == "local" and args.route != "gemini":
                raise BridgeError("local_pair_has_no_t3_route")
            pair["route"] = args.route
            pair["updatedAt"] = now_iso()
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
    sources = sum(bool(x) for x in (args.message is not None, args.file is not None, args.stdin))
    if sources != 1:
        raise BridgeError("specify_exactly_one_message_source")
    if args.message is not None:
        message = args.message
    elif args.file is not None:
        message = Path(args.file).read_text(encoding="utf-8")
    else:
        message = sys.stdin.read()

    res = bridge.submit_task(
        pair_id=args.pair,
        message=message,
        task_id=getattr(args, "task_id", None),
        reply_to=getattr(args, "reply_to", None),
        idempotency_key=getattr(args, "idempotency_key", None),
        forbid_legacy_pair_uid_deferral=False,
    )
    if res.get("routed") == "t3" and not res.get("idempotentReplay"):
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    if not getattr(args, "wait", False):
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    rc, out = bridge.wait_for_task(
        res["taskId"],
        timeout_s=float(getattr(args, "timeout", 300.0)),
        return_on_attention=bool(getattr(args, "return_on_attention", False)),
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return rc


def cmd_task(bridge: Bridge, args: argparse.Namespace) -> int:
    bridge.setup()
    if args.task_cmd == "status":
        out = bridge.get_task_status(args.task_id)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if args.task_cmd == "wait":
        rc, out = bridge.wait_for_task(
            args.task_id,
            timeout_s=float(args.timeout),
            return_on_attention=bool(args.return_on_attention),
        )
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return rc
    if args.task_cmd == "read":
        out = bridge.read_task_payload(args.task_id)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    raise BridgeError("unknown_task_subcommand")


def cmd_mcp(bridge: Bridge, args: argparse.Namespace) -> int:
    import mcp_server  # noqa: E402
    return mcp_server.run_stdio_server(
        bridge=bridge,
        allow_pairs_raw=getattr(args, "allow_pairs", None),
        adapter_filter=getattr(args, "adapter_filter", None),
        scope_name=getattr(args, "scope", None),
    )


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
    label, plist_path = _validated_label(root=bridge.root)
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
        "GEMINI_TASK_BRIDGE_ROOT": str(bridge.root),
    }
    if label != DEFAULT_LABEL or bridge_env("LABEL"):
        env_vars["GEMINI_TASK_BRIDGE_LABEL"] = label
    if cli_override := bridge_env("CLI"):
        env_vars["GEMINI_TASK_BRIDGE_CLI"] = cli_override
    if bridge.t3_home != str(Path(home_dir) / ".t3") or bridge_env("T3_HOME"):
        env_vars["GEMINI_TASK_BRIDGE_T3_HOME"] = bridge.t3_home

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
    if agent_info(label)["loaded"]:
        subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True, timeout=15)
    fd = os.open(str(plist_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(plistlib.dumps(data))
        stream.flush()
        os.fsync(stream.fileno())
    p = subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)], capture_output=True, timeout=15)
    if p.returncode != 0:
        raise BridgeError("launchagent_bootstrap_failed")
    print(json.dumps({"installed": str(plist_path), "label": label, "pythonExecutable": py_exec, "agent": agent_info(label)}, ensure_ascii=False))
    return 0


def uninstall_agent(root: Path) -> int:
    label, plist_path = _validated_label(root=root)
    info = agent_info(label)
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


def restart_agent(root: Path) -> int:
    label, _ = _validated_label(root=root)
    p = subprocess.run(["/bin/launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"], capture_output=True, text=True, timeout=15)
    if p.returncode != 0:
        raise BridgeError("launchagent_kickstart_failed")
    print(json.dumps({"restarted": True, "label": label, "agent": agent_info(label)}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(resolve_default_root()), help="Bridge root directory")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_doc = sub.add_parser("doctor", help="Check AX, Gemini.app, T3 CLI, and LaunchAgent health")
    p_doc.add_argument("--json", action="store_true")

    p_st = sub.add_parser("status", help="Show bridge queue and pair routing status")
    p_st.add_argument("--json", action="store_true")

    p_mode = sub.add_parser("mode", help="Switch global mode (hybrid / t3-all / gemini-all)")
    p_mode.add_argument("target_mode", choices=("hybrid", "t3-all", "gemini-all"))

    p_pair = sub.add_parser("pair", help="Manage 1-to-1 agent <-> Gemini conversation pairs")
    pair_sub = p_pair.add_subparsers(dest="pair_cmd", required=True)

    p_pl = pair_sub.add_parser("list", help="List configured 1-to-1 pairs")
    p_pl.add_argument("--json", action="store_true")

    p_gc = pair_sub.add_parser("gemini-chats", help="List conversations currently visible in Gemini.app")
    p_gc.add_argument("--json", action="store_true")

    p_pa = pair_sub.add_parser("add", help="Bind a local agent or T3 thread to an existing Gemini conversation title")
    p_pa.add_argument("--id", required=True)
    p_pa.add_argument("--adapter", choices=("t3", "local"), default=None)
    p_pa.add_argument("--t3-thread")
    p_pa.add_argument("--gemini-title", required=True)
    p_pa.add_argument("--reply-to")
    p_pa.add_argument("--label")
    p_pa.add_argument("--workspace")
    p_pa.add_argument("--route", choices=("gemini", "t3"), default="gemini")
    p_pa.add_argument("--reply-mode", choices=("auto", "outbox", "ax_capture"), default="auto")
    p_pa.add_argument("--force", action="store_true")

    p_pi = pair_sub.add_parser("init", help="Create a new Gemini Task conversation and bind 1-to-1")
    p_pi.add_argument("--id", required=True)
    p_pi.add_argument("--adapter", choices=("t3", "local"), default=None)
    p_pi.add_argument("--t3-thread")
    p_pi.add_argument("--gemini-title", required=True)
    p_pi.add_argument("--reply-to")
    p_pi.add_argument("--label")
    p_pi.add_argument("--workspace")
    p_pi.add_argument("--init-prompt")
    p_pi.add_argument("--reply-mode", choices=("auto", "outbox", "ax_capture"), default="auto")
    p_pi.add_argument("--wait-sec", type=float, default=35.0)

    for p_control in (p_gc, p_pi):
        p_control.add_argument("--direct", action="store_true")
        p_control.add_argument("--idempotency-key")
        p_control.add_argument("--no-wait", action="store_true")
    p_pi.add_argument("--json", action="store_true")

    p_req = pair_sub.add_parser("request", help="Inspect or wait for a daemon control request")
    p_req.add_argument("request_id")
    p_req.add_argument("--wait", type=float, default=0.0)
    p_req.add_argument("--json", action="store_true")

    p_pr = pair_sub.add_parser("route", help="Switch a T3 pair between t3 and gemini")
    p_pr.add_argument("id")
    p_pr.add_argument("route", choices=("t3", "gemini"))

    p_prm = pair_sub.add_parser("remove", help="Remove a pair mapping")
    p_prm.add_argument("id")
    p_prm.add_argument("--force", action="store_true")

    p_send = sub.add_parser("send", help="Send a task via configured 1-to-1 pair")
    p_send.add_argument("--pair", required=True)
    p_send.add_argument("--message")
    p_send.add_argument("--file")
    p_send.add_argument("--stdin", action="store_true")
    p_send.add_argument("--task-id")
    p_send.add_argument("--reply-to")
    p_send.add_argument("--idempotency-key")
    p_send.add_argument("--wait", action="store_true")
    p_send.add_argument("--return-on-attention", action="store_true")
    p_send.add_argument("--timeout", type=float, default=300.0)
    p_send.add_argument("--json", action="store_true")

    p_task = sub.add_parser("task", help="Inspect, wait on, or read completed bridge tasks")
    task_sub = p_task.add_subparsers(dest="task_cmd", required=True)

    p_ts = task_sub.add_parser("status", help="Inspect detailed task status")
    p_ts.add_argument("task_id")
    p_ts.add_argument("--json", action="store_true")

    p_tw = task_sub.add_parser("wait", help="Wait for a task to finish and return verified reply")
    p_tw.add_argument("task_id")
    p_tw.add_argument("--timeout", type=float, default=300.0)
    p_tw.add_argument("--return-on-attention", action="store_true")
    p_tw.add_argument("--json", action="store_true")

    p_tr = task_sub.add_parser("read", help="Read archived Gemini reply payload for a completed task")
    p_tr.add_argument("task_id")
    p_tr.add_argument("--json", action="store_true")

    p_mcp = sub.add_parser("mcp", help="Run zero-dependency stdio MCP server")
    p_mcp.add_argument("--allow-pairs", default=None)
    p_mcp.add_argument("--scope", default=None)
    p_mcp.add_argument("--adapter-filter", choices=("local", "t3", "all"), default=None)

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
    if args.cmd != "mcp":
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
        if args.cmd == "task":
            return cmd_task(bridge, args)
        if args.cmd == "mcp":
            return cmd_mcp(bridge, args)
        if args.cmd == "run":
            return bridge.execute(continuous=True)
        if args.cmd == "once":
            return bridge.execute(continuous=False)
        if args.cmd == "service":
            if args.svc_cmd == "install":
                return install_agent(bridge)
            if args.svc_cmd == "uninstall":
                return uninstall_agent(bridge.root)
            if args.svc_cmd == "restart":
                return restart_agent(bridge.root)
            if args.svc_cmd == "status":
                print(json.dumps({"agent": agent_info(root=bridge.root), "daemonHealth": bridge.read_health()}, ensure_ascii=False, indent=2))
                return 0
        return 2
    except Exception as exc:
        err = str(exc) if isinstance(exc, BridgeError) else f"{type(exc).__name__}:{exc}"
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    # Runtime modules import the same BridgeError and locks when invoked as a script.
    sys.modules.setdefault("gemini_task_bridge", sys.modules[__name__])
    sys.exit(main())
