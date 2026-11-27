#!/usr/bin/python3
"""macOS Accessibility (AXUIElement) and diagnostic-log driver for Gemini.app.

Python 3.9+ standard library only (zero external pip dependencies).
"""
from __future__ import annotations

import ctypes
import ctypes.util
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable


NEW_CHAT_TITLES = frozenset({"新对话", "New chat", "New Chat", ""})
SIDEBAR_STATIC_LABELS = frozenset({
    "库", "图片", "已排定", "学生", "更多", "笔记本", "项目", "排程", "技能",
    "Library", "Images", "Scheduled", "Students", "More", "Notebooks",
    "Projects", "Schedules", "Skills",
})
CHROME_TEXTS = frozenset({
    "Gemini 是 AI，可能会出错。",
    "Gemini is AI and can make mistakes.",
    "问问 Gemini",
    "分配任务",
    "描述任务",
    "Ask Gemini",
    "Assign a task",
    "Describe a task",
    "Message Gemini",
    "附加",
    "共享屏幕",
    "使用麦克风",
    "Speak to Window",
    "对话",
    "任务",
    "Chat",
    "Task",
    "Folders",
    "文件夹",
    "拷贝",
    "Copy",
    "Copy response",
    "提交",
    "Submit",
    "Send",
    "停止回答",
    "停止",
    "Stop",
    "Stop response",
    "What’s next?",
    "What's next?",
    "接下来做什么？",
    "Tag me in! What’s next?",
    "Tag me in! What's next?",
    "Gemini",
})

CONFIRMATION_CARD_IDS = (
    "ActionCardRemyConfirmationCard",
    "ActionCardRemyElicitationCard",
    "AgentWaitingForInputRow",
    "InlineConfirmationSlotView",
    "tab_stop_Continue",
)

DIAGNOSTIC_START_MARKERS = (
    "addStreamingConversation",
    "agentModeTask: started",
    "agentModeTask: planReceived",
    "finishSending",
)
DIAGNOSTIC_ACTIVITY_MARKERS = (
    "addStreamingConversation",
    "agentModeTask: started",
    "agentModeTask: planReceived",
    "finishSending",
    "Executing function call",
)
DIAGNOSTIC_DONE_MARKERS = (
    "removeStreamingConversation()",
    "agentModeTask: succeeded",
    "agentModeTask: ended",
)

_BADGE_MAP = {
    "进行中": "in_progress",
    "In progress": "in_progress",
    "需要注意": "needs_attention",
    "Needs attention": "needs_attention",
    "准备就绪": "ready",
    "Ready": "ready",
}
_BADGE_ALT = "|".join(re.escape(k) for k in sorted(_BADGE_MAP, key=len, reverse=True))
_QUOTED_BADGE_ZH_RE = re.compile(r"^“(.+)”，(" + _BADGE_ALT + r")$")
_QUOTED_BADGE_EN_RE = re.compile(r'^"(.+)",\s*(' + _BADGE_ALT + r")$")
_PLAIN_BADGE_RE = re.compile(r"^(.+?)[，,]\s*(" + _BADGE_ALT + r")$")


def normalize_window_title(raw: str) -> str:
    """Strip Gemini desktop window prefix and whitespace."""
    text = (raw or "").strip()
    if text == "Gemini":
        return ""
    for prefix in ("Gemini - ", "Gemini — ", "Gemini – ", "Gemini: "):
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def parse_sidebar_row_label(raw: str) -> dict[str, Any]:
    """Parse Gemini sidebar row accessibility label (zh_CN + en) into structured fields."""
    text = (raw or "").strip()
    pinned = False
    badge = None

    for suffix in ("，已固定", ", Pinned"):
        if text.endswith(suffix):
            pinned = True
            text = text[: -len(suffix)].rstrip()
            break

    m = _QUOTED_BADGE_ZH_RE.match(text)
    if m:
        title = m.group(1).strip()
        badge = _BADGE_MAP[m.group(2)]
    else:
        m = _QUOTED_BADGE_EN_RE.match(text)
        if m:
            title = m.group(1).strip()
            badge = _BADGE_MAP[m.group(2)]
        else:
            m = _PLAIN_BADGE_RE.match(text)
            if m:
                title = m.group(1).strip()
                badge = _BADGE_MAP[m.group(2)]
            else:
                title = text

    if not pinned:
        for suffix in ("，已固定", ", Pinned"):
            if title.endswith(suffix):
                pinned = True
                title = title[: -len(suffix)].rstrip()
                break

    for suffix in (", On this Mac", "，在此 Mac 上"):
        if title.endswith(suffix):
            title = title[: -len(suffix)].rstrip()
            break

    return {
        "raw": raw,
        "title": title,
        "pinned": pinned,
        "badge": badge,
    }


def _load_frameworks() -> tuple[Any, Any, Any]:
    cf_path = ctypes.util.find_library("CoreFoundation") or "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    ax_path = ctypes.util.find_library("ApplicationServices") or "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
    cg_path = ctypes.util.find_library("CoreGraphics") or "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
    cf = ctypes.cdll.LoadLibrary(cf_path)
    ax = ctypes.cdll.LoadLibrary(ax_path)
    try:
        cg = ctypes.cdll.LoadLibrary(cg_path)
    except OSError:
        cg = None

    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
    cf.CFStringGetLength.restype = ctypes.c_long
    cf.CFStringGetMaximumSizeForEncoding.argtypes = [ctypes.c_long, ctypes.c_uint32]
    cf.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
    cf.CFGetTypeID.restype = ctypes.c_ulong
    cf.CFStringGetTypeID.restype = ctypes.c_ulong
    cf.CFArrayGetTypeID.restype = ctypes.c_ulong
    cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
    cf.CFArrayGetCount.restype = ctypes.c_long
    cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
    cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
    cf.CFBooleanGetTypeID.restype = ctypes.c_ulong
    cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
    cf.CFBooleanGetValue.restype = ctypes.c_bool
    cf.CFDictionaryGetTypeID.restype = ctypes.c_ulong
    cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    cf.CFDictionaryGetValue.restype = ctypes.c_void_p
    cf.CFNumberGetTypeID.restype = ctypes.c_ulong
    cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    cf.CFNumberGetValue.restype = ctypes.c_bool
    cf.CFRetain.argtypes = [ctypes.c_void_p]
    cf.CFRetain.restype = ctypes.c_void_p
    cf.CFRelease.argtypes = [ctypes.c_void_p]

    ax.AXIsProcessTrusted.restype = ctypes.c_bool
    ax.AXUIElementCreateApplication.argtypes = [ctypes.c_int32]
    ax.AXUIElementCreateApplication.restype = ctypes.c_void_p
    ax.AXUIElementSetMessagingTimeout.argtypes = [ctypes.c_void_p, ctypes.c_float]
    ax.AXUIElementSetMessagingTimeout.restype = ctypes.c_int32
    ax.AXUIElementCopyAttributeValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    ax.AXUIElementCopyAttributeValue.restype = ctypes.c_int32
    ax.AXUIElementSetAttributeValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    ax.AXUIElementSetAttributeValue.restype = ctypes.c_int32
    ax.AXUIElementCopyActionNames.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    ax.AXUIElementCopyActionNames.restype = ctypes.c_int32
    ax.AXUIElementPerformAction.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    ax.AXUIElementPerformAction.restype = ctypes.c_int32

    if cg is not None and hasattr(cg, "CGSessionCopyCurrentDictionary"):
        cg.CGSessionCopyCurrentDictionary.argtypes = []
        cg.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p

    return cf, ax, cg


def is_ax_trusted() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        _, ax, _ = _load_frameworks()
        return bool(ax.AXIsProcessTrusted())
    except Exception:
        return False


def is_screen_locked() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        cf, _, cg = _load_frameworks()
        if cg is None or not hasattr(cg, "CGSessionCopyCurrentDictionary"):
            return False
        d = cg.CGSessionCopyCurrentDictionary()
        if not d:
            return False
        try:
            k = cf.CFStringCreateWithCString(None, b"CGSSessionScreenIsLocked", 0x08000100)
            if not k:
                return False
            try:
                val = cf.CFDictionaryGetValue(d, k)
                if not val:
                    return False
                tid = cf.CFGetTypeID(val)
                if tid == cf.CFBooleanGetTypeID():
                    return bool(cf.CFBooleanGetValue(val))
                if tid == cf.CFNumberGetTypeID():
                    num = ctypes.c_int(0)
                    if cf.CFNumberGetValue(val, 9, ctypes.byref(num)):  # kCFNumberIntType = 9
                        return num.value != 0
                return False
            finally:
                cf.CFRelease(k)
        finally:
            cf.CFRelease(d)
    except Exception:
        return False


def find_gemini_pid() -> int | None:
    if sys.platform != "darwin":
        return None
    try:
        p = subprocess.run(["/usr/bin/pgrep", "-x", "Gemini"], capture_output=True, text=True, timeout=3)
        if p.returncode == 0:
            for line in p.stdout.splitlines():
                s = line.strip()
                if s.isdigit() and int(s) > 0:
                    return int(s)
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def pick_main_window_index(candidates: list[dict[str, Any]]) -> int | None:
    """Pick the real Gemini Omnichat window index, ignoring search popups (title=='') and dialogs."""
    best_p2: int | None = None
    best_p3: int | None = None
    best_p4: int | None = None
    for idx, c in enumerate(candidates):
        if c.get("role") != "AXWindow" or c.get("is_minichat"):
            continue
        sub = str(c.get("subrole") or "")
        title = str(c.get("title") or "").strip()
        if sub == "AXStandardWindow" and title.startswith("Gemini"):
            return idx
        if sub == "AXStandardWindow" and title:
            if best_p2 is None:
                best_p2 = idx
        elif sub != "AXDialog" and title:
            if best_p3 is None:
                best_p3 = idx
        elif sub == "AXStandardWindow" and best_p4 is None:
            best_p4 = idx
    return best_p2 if best_p2 is not None else (best_p3 if best_p3 is not None else best_p4)


class MacOSAXBridge:
    """Thread-safe CoreFoundation/ApplicationServices AX wrapper for Gemini.app."""

    _K_CF_STRING_UTF8 = 0x08000100
    _MAX_NODES = 1400
    _MAX_DEPTH = 24
    _singleton: "MacOSAXBridge | None" = None

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("macos_only")
        self.cf, self.ax, self.cg = _load_frameworks()
        self._attrs = {
            name: self._cfstr(name)
            for name in (
                "AXWindows",
                "AXChildren",
                "AXRows",
                "AXRole",
                "AXSubrole",
                "AXIdentifier",
                "AXDescription",
                "AXTitle",
                "AXValue",
                "AXFocused",
                "AXEnabled",
                "AXSelected",
                "AXFrontmost",
                "AXHidden",
                "AXMenuBar",
                "AXSheets",
                "AXPress",
                "AXCancel",
                "AXShowMenu",
            )
        }
        self._k_true = ctypes.c_void_p.in_dll(self.cf, "kCFBooleanTrue").value
        self._k_false = ctypes.c_void_p.in_dll(self.cf, "kCFBooleanFalse").value

    @classmethod
    def get_instance(cls) -> "MacOSAXBridge | None":
        if sys.platform != "darwin":
            return None
        if cls._singleton is None:
            try:
                cls._singleton = cls()
            except Exception:
                return None
        return cls._singleton

    def _cfstr(self, text: str) -> int:
        ref = self.cf.CFStringCreateWithCString(None, text.encode("utf-8"), self._K_CF_STRING_UTF8)
        if not ref:
            raise RuntimeError("CFStringCreateWithCString_failed")
        return int(ref)

    def _attr(self, name: str) -> int:
        if name in self._attrs:
            return self._attrs[name]
        ref = self._cfstr(name)
        self._attrs[name] = ref
        return ref

    def _copy_attr(self, el: int, name: str) -> int | None:
        out = ctypes.c_void_p()
        err = int(self.ax.AXUIElementCopyAttributeValue(el, self._attr(name), ctypes.byref(out)))
        if err != 0 or not out.value:
            return None
        return int(out.value)

    def _get_str(self, el: int, name: str) -> str:
        ref = self._copy_attr(el, name)
        if not ref:
            return ""
        try:
            if self.cf.CFGetTypeID(ref) != self.cf.CFStringGetTypeID():
                return ""
            length = int(self.cf.CFStringGetLength(ref))
            cap = max(64, int(self.cf.CFStringGetMaximumSizeForEncoding(length, self._K_CF_STRING_UTF8)) + 1)
            buf = ctypes.create_string_buffer(cap)
            if self.cf.CFStringGetCString(ref, buf, cap, self._K_CF_STRING_UTF8):
                return buf.value.decode("utf-8", errors="replace")
            return ""
        finally:
            self.cf.CFRelease(ref)

    def _get_bool(self, el: int, name: str, default: bool = False) -> bool:
        ref = self._copy_attr(el, name)
        if not ref:
            return default
        try:
            if self.cf.CFGetTypeID(ref) == self.cf.CFBooleanGetTypeID():
                return bool(self.cf.CFBooleanGetValue(ref))
            return default
        finally:
            self.cf.CFRelease(ref)

    def _get_actions(self, el: int) -> list[str]:
        out = ctypes.c_void_p()
        err = int(self.ax.AXUIElementCopyActionNames(el, ctypes.byref(out)))
        if err != 0 or not out.value:
            return []
        actions: list[str] = []
        try:
            if self.cf.CFGetTypeID(out.value) == self.cf.CFArrayGetTypeID():
                n = int(self.cf.CFArrayGetCount(out.value))
                str_tid = self.cf.CFStringGetTypeID()
                for i in range(n):
                    item = int(self.cf.CFArrayGetValueAtIndex(out.value, i))
                    if self.cf.CFGetTypeID(item) == str_tid:
                        length = int(self.cf.CFStringGetLength(item))
                        cap = max(64, int(self.cf.CFStringGetMaximumSizeForEncoding(length, self._K_CF_STRING_UTF8)) + 1)
                        buf = ctypes.create_string_buffer(cap)
                        if self.cf.CFStringGetCString(item, buf, cap, self._K_CF_STRING_UTF8):
                            actions.append(buf.value.decode("utf-8", errors="replace"))
        finally:
            self.cf.CFRelease(out.value)
        return actions

    def perform_action(self, el: int, action: str = "AXPress") -> int:
        return int(self.ax.AXUIElementPerformAction(el, self._attr(action)))

    def _app_el(self, pid: int, timeout_s: float = 1.5) -> int:
        app_el = int(self.ax.AXUIElementCreateApplication(int(pid)))
        self.ax.AXUIElementSetMessagingTimeout(app_el, float(timeout_s))
        return app_el

    def _is_minichat_window(self, win_el: int) -> bool:
        sub = self._get_str(win_el, "AXSubrole")
        ident = self._get_str(win_el, "AXIdentifier").lower()
        title = self._get_str(win_el, "AXTitle").lower()
        if sub == "AXSystemDialog":
            return True
        if "minichat" in ident or "mini chat" in title or "迷你对话" in title:
            return True
        return False

    def _find_main_window(self, app_el: int) -> int | None:
        wins_ref = self._copy_attr(app_el, "AXWindows")
        if not wins_ref:
            return None
        try:
            if self.cf.CFGetTypeID(wins_ref) != self.cf.CFArrayGetTypeID():
                return None
            count = int(self.cf.CFArrayGetCount(wins_ref))
            win_handles: list[int] = []
            candidates: list[dict[str, Any]] = []
            for i in range(count):
                w = int(self.cf.CFArrayGetValueAtIndex(wins_ref, i))
                win_handles.append(w)
                candidates.append({
                    "role": self._get_str(w, "AXRole"),
                    "subrole": self._get_str(w, "AXSubrole"),
                    "title": self._get_str(w, "AXTitle"),
                    "is_minichat": self._is_minichat_window(w),
                })
            chosen_idx = pick_main_window_index(candidates)
            if chosen_idx is not None:
                w = win_handles[chosen_idx]
                self.cf.CFRetain(w)
                return w
            return None
        finally:
            self.cf.CFRelease(wins_ref)

    def close_search_popup(self, pid: int) -> bool:
        """Close Gemini's untitled floating search window if open."""
        app_el = self._app_el(pid)
        closed = False
        try:
            wins_ref = self._copy_attr(app_el, "AXWindows")
            if not wins_ref:
                return False
            try:
                if self.cf.CFGetTypeID(wins_ref) != self.cf.CFArrayGetTypeID():
                    return False
                for i in range(int(self.cf.CFArrayGetCount(wins_ref))):
                    w = int(self.cf.CFArrayGetValueAtIndex(wins_ref, i))
                    if self._get_str(w, "AXSubrole") == "AXStandardWindow" and not self._get_str(w, "AXTitle").strip():
                        btn = self._find_element(
                            w,
                            lambda el, r: r == "AXButton" and self._get_str(el, "AXIdentifier") == "gemini_search_close_button",
                            skip_outline=False,
                            max_depth=10,
                        )
                        if btn is not None:
                            try:
                                if self.perform_action(btn, "AXPress") == 0:
                                    closed = True
                            finally:
                                self.cf.CFRelease(btn)
            finally:
                self.cf.CFRelease(wins_ref)
        finally:
            self.cf.CFRelease(app_el)
        return closed

    def _walk(
        self,
        root_el: int,
        visitor: Callable[[int, str], bool],
        *,
        skip_outline: bool = True,
        max_depth: int | None = None,
        max_nodes: int | None = None,
    ) -> None:
        visited = 0
        depth_limit = self._MAX_DEPTH if max_depth is None else max_depth
        node_limit = self._MAX_NODES if max_nodes is None else max_nodes
        arr_tid = self.cf.CFArrayGetTypeID()

        def _rec(el: int, depth: int) -> bool:
            nonlocal visited
            if depth > depth_limit or visited >= node_limit:
                return False
            visited += 1
            role = self._get_str(el, "AXRole")
            if skip_outline and role == "AXOutline":
                return False
            if visitor(el, role):
                return True
            ch_ref = self._copy_attr(el, "AXChildren")
            if ch_ref:
                try:
                    if self.cf.CFGetTypeID(ch_ref) == arr_tid:
                        n = int(self.cf.CFArrayGetCount(ch_ref))
                        for idx in range(min(n, 200)):
                            child = int(self.cf.CFArrayGetValueAtIndex(ch_ref, idx))
                            if _rec(child, depth + 1):
                                return True
                finally:
                    self.cf.CFRelease(ch_ref)
            return False

        _rec(root_el, 0)

    def _find_element(
        self,
        root_el: int,
        predicate: Callable[[int, str], bool],
        *,
        skip_outline: bool = True,
        max_depth: int | None = None,
    ) -> int | None:
        found: int | None = None

        def _visit(el: int, role: str) -> bool:
            nonlocal found
            if predicate(el, role):
                self.cf.CFRetain(el)
                found = el
                return True
            return False

        self._walk(root_el, _visit, skip_outline=skip_outline, max_depth=max_depth)
        return found

    def _collect_texts(self, root_el: int, max_depth: int = 3) -> list[str]:
        texts: list[str] = []

        def _visit(el: int, _role: str) -> bool:
            for attr in ("AXValue", "AXTitle", "AXDescription"):
                val = self._get_str(el, attr).strip()
                if val and val not in texts:
                    texts.append(val)
            return False

        self._walk(root_el, _visit, skip_outline=False, max_depth=max_depth, max_nodes=40)
        return texts

    def _has_descendant_role(self, root_el: int, role_name: str, max_depth: int = 3) -> bool:
        hit = False

        def _visit(el: int, role: str) -> bool:
            nonlocal hit
            if el != root_el and role == role_name:
                hit = True
                return True
            return False

        self._walk(root_el, _visit, skip_outline=False, max_depth=max_depth, max_nodes=40)
        return hit

    def is_hidden(self, pid: int) -> bool:
        app_el = self._app_el(pid)
        try:
            return self._get_bool(app_el, "AXHidden", False)
        finally:
            self.cf.CFRelease(app_el)

    def set_hidden(self, pid: int, hidden: bool) -> bool:
        app_el = self._app_el(pid)
        try:
            flag = self._k_true if hidden else self._k_false
            return int(self.ax.AXUIElementSetAttributeValue(app_el, self._attr("AXHidden"), flag)) == 0
        finally:
            self.cf.CFRelease(app_el)

    def has_usable_main_window(self, pid: int) -> bool:
        app_el = self._app_el(pid)
        try:
            win = self._find_main_window(app_el)
            if win is not None:
                self.cf.CFRelease(win)
                return True
            return False
        finally:
            self.cf.CFRelease(app_el)

    def get_window_title(self, pid: int) -> str | None:
        app_el = self._app_el(pid)
        try:
            win = self._find_main_window(app_el)
            if not win:
                return None
            try:
                return normalize_window_title(self._get_str(win, "AXTitle"))
            finally:
                self.cf.CFRelease(win)
        finally:
            self.cf.CFRelease(app_el)

    def ensure_main_window_silent(
        self,
        pid: int,
        keep_hidden: bool | None = None,
        wait_s: float = 2.5,
    ) -> tuple[bool, bool]:
        """Ensure Gemini Omnichat AXStandardWindow exists without stealing focus."""
        if keep_hidden is None:
            keep_hidden = self.is_hidden(pid)
        if self.has_usable_main_window(pid):
            if keep_hidden:
                self.set_hidden(pid, True)
            return True, False

        app_el = self._app_el(pid)
        try:
            mb = self._copy_attr(app_el, "AXMenuBar")
            if not mb:
                return False, False
            try:
                file_menu = self._find_element(
                    mb,
                    lambda el, r: r == "AXMenuBarItem" and self._get_str(el, "AXTitle").strip().lower() in ("文件", "file"),
                    skip_outline=False,
                    max_depth=3,
                )
                root = file_menu if file_menu is not None else mb
                try:
                    open_item = self._find_element(
                        root,
                        lambda el, r: r == "AXMenuItem" and self._get_str(el, "AXTitle").strip().lower() in ("打开应用", "open app"),
                        skip_outline=False,
                        max_depth=5,
                    )
                    if open_item is None:
                        return False, False
                    try:
                        if self.perform_action(open_item, "AXPress") != 0:
                            return False, False
                    finally:
                        self.cf.CFRelease(open_item)
                finally:
                    if file_menu is not None:
                        self.cf.CFRelease(file_menu)
            finally:
                self.cf.CFRelease(mb)
        finally:
            self.cf.CFRelease(app_el)

        deadline = time.monotonic() + max(0.3, wait_s)
        while time.monotonic() < deadline:
            if keep_hidden:
                self.set_hidden(pid, True)
            if self.has_usable_main_window(pid):
                if keep_hidden:
                    self.set_hidden(pid, True)
                return True, True
            time.sleep(0.05)
        return False, False

    def _find_sidebar_outline(self, win_el: int) -> int | None:
        return self._find_element(
            win_el,
            lambda _el, r: r == "AXOutline",
            skip_outline=False,
            max_depth=10,
        )

    def _ensure_sidebar_open(self, pid: int, win_el: int) -> int | None:
        outline = self._find_sidebar_outline(win_el)
        if outline is not None:
            return outline
        toggle = self._find_element(
            win_el,
            lambda el, r: r == "AXButton" and self._get_str(el, "AXIdentifier") == "sidebar_toggle",
            skip_outline=False,
            max_depth=8,
        )
        if toggle is not None:
            try:
                self.perform_action(toggle, "AXPress")
            finally:
                self.cf.CFRelease(toggle)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                outline = self._find_sidebar_outline(win_el)
                if outline is not None:
                    return outline
                time.sleep(0.05)
        return None

    def _iter_sidebar_buttons(self, outline_el: int) -> list[tuple[int, dict[str, Any]]] | None:
        """Return retained buttons, or None when AX did not expose readable rows."""
        rows_ref = self._copy_attr(outline_el, "AXRows") or self._copy_attr(outline_el, "AXChildren")
        if not rows_ref:
            return None
        results: list[tuple[int, dict[str, Any]]] = []
        seen_filter = False
        try:
            if self.cf.CFGetTypeID(rows_ref) != self.cf.CFArrayGetTypeID():
                return None
            cnt = int(self.cf.CFArrayGetCount(rows_ref))
            for i in range(cnt):
                row = int(self.cf.CFArrayGetValueAtIndex(rows_ref, i))
                has_filter = self._find_element(
                    row,
                    lambda el, _r: self._get_str(el, "AXIdentifier") == "sidebar.taskFilterMenu",
                    skip_outline=False,
                    max_depth=4,
                )
                if has_filter is not None:
                    self.cf.CFRelease(has_filter)
                    seen_filter = True
                    continue
                btn = self._find_element(
                    row,
                    lambda _el, r: r == "AXButton",
                    skip_outline=False,
                    max_depth=4,
                )
                if btn is None:
                    continue
                desc = self._get_str(btn, "AXDescription").strip()
                title_attr = self._get_str(btn, "AXTitle").strip()
                raw = desc or title_attr
                if not raw:
                    self.cf.CFRelease(btn)
                    continue
                parsed = parse_sidebar_row_label(raw)
                actions = self._get_actions(btn)
                if parsed["title"] in SIDEBAR_STATIC_LABELS and "AXShowMenu" not in actions:
                    self.cf.CFRelease(btn)
                    continue
                if seen_filter or "AXShowMenu" in actions:
                    results.append((btn, parsed))
                else:
                    self.cf.CFRelease(btn)
            return results
        finally:
            self.cf.CFRelease(rows_ref)

    def _list_menubar_conversations(self, app_el: int) -> list[str]:
        mb = self._copy_attr(app_el, "AXMenuBar")
        if not mb:
            return []
        skip_items = {
            "近期对话", "查看所有对话", "搜索",
            "recent chats", "view all chats", "search",
        }
        names: list[str] = []
        try:
            chat_menu = self._find_element(
                mb,
                lambda el, r: r == "AXMenuBarItem" and self._get_str(el, "AXTitle").strip().lower() in ("对话", "chats", "conversations"),
                skip_outline=False,
                max_depth=3,
            )
            if chat_menu is None:
                return []
            try:
                def _visit(el: int, role: str) -> bool:
                    if role == "AXMenuItem":
                        t = self._get_str(el, "AXTitle").strip()
                        if t and t.lower() not in skip_items and t not in names:
                            names.append(t)
                    return False

                self._walk(chat_menu, _visit, skip_outline=False, max_depth=4, max_nodes=100)
            finally:
                self.cf.CFRelease(chat_menu)
        finally:
            self.cf.CFRelease(mb)
        return names

    def list_conversations(self, pid: int) -> dict[str, Any]:
        app_el = self._app_el(pid)
        try:
            win = self._find_main_window(app_el)
            current_title = None
            sidebar_readable = False
            items: list[dict[str, Any]] = []
            seen_titles: set[str] = set()
            if win is not None:
                try:
                    current_title = normalize_window_title(self._get_str(win, "AXTitle"))
                    outline = self._find_sidebar_outline(win)
                    if outline is not None:
                        try:
                            btns = self._iter_sidebar_buttons(outline)
                            sidebar_readable = btns is not None
                            btns = btns or []
                            try:
                                for _btn, parsed in btns:
                                    t = parsed["title"]
                                    if t and t not in seen_titles:
                                        seen_titles.add(t)
                                        parsed["source"] = "sidebar"
                                        items.append(parsed)
                            finally:
                                for btn, _ in btns:
                                    self.cf.CFRelease(btn)
                        finally:
                            self.cf.CFRelease(outline)
                finally:
                    self.cf.CFRelease(win)

            for title in self._list_menubar_conversations(app_el):
                if title not in seen_titles:
                    seen_titles.add(title)
                    items.append({"raw": title, "title": title, "pinned": False, "badge": None, "source": "menubar"})

            return {
                "hasWindow": win is not None,
                "sidebarReadable": sidebar_readable,
                "currentTitle": current_title,
                "conversations": items,
            }
        finally:
            self.cf.CFRelease(app_el)

    def _press_menubar_conversation(self, app_el: int, target_title: str) -> bool:
        mb = self._copy_attr(app_el, "AXMenuBar")
        if not mb:
            return False
        try:
            chat_menu = self._find_element(
                mb,
                lambda el, r: r == "AXMenuBarItem" and self._get_str(el, "AXTitle").strip().lower() in ("对话", "chats", "conversations"),
                skip_outline=False,
                max_depth=3,
            )
            if chat_menu is None:
                return False
            try:
                item = self._find_element(
                    chat_menu,
                    lambda el, r: r == "AXMenuItem" and self._get_str(el, "AXTitle").strip() == target_title,
                    skip_outline=False,
                    max_depth=4,
                )
                if item is None:
                    return False
                try:
                    return self.perform_action(item, "AXPress") == 0
                finally:
                    self.cf.CFRelease(item)
            finally:
                self.cf.CFRelease(chat_menu)
        finally:
            self.cf.CFRelease(mb)

    def _search_and_open_conversation(self, app_el: int, win_el: int, target_title: str) -> bool:
        def _find_search_field() -> int | None:
            wins_ref = self._copy_attr(app_el, "AXWindows")
            if wins_ref:
                try:
                    if self.cf.CFGetTypeID(wins_ref) == self.cf.CFArrayGetTypeID():
                        for i in range(int(self.cf.CFArrayGetCount(wins_ref))):
                            w = int(self.cf.CFArrayGetValueAtIndex(wins_ref, i))
                            f = self._find_element(
                                w,
                                lambda el, r: r in ("AXTextField", "AXSearchField") and self._get_str(el, "AXIdentifier") == "gemini_search_text_field",
                                skip_outline=False,
                                max_depth=10,
                            )
                            if f is not None:
                                return f
                finally:
                    self.cf.CFRelease(wins_ref)
            return self._find_element(
                win_el,
                lambda el, r: r in ("AXTextField", "AXSearchField") and self._get_str(el, "AXIdentifier") != "settings_search_field",
                skip_outline=False,
                max_depth=12,
            )

        field = _find_search_field()
        if field is None:
            search_btn = self._find_element(
                win_el,
                lambda el, r: r == "AXButton" and self._get_str(el, "AXIdentifier") == "sidebar_header_search",
                skip_outline=False,
                max_depth=10,
            )
            if search_btn is None:
                return False
            try:
                if self.perform_action(search_btn, "AXPress") != 0:
                    return False
            finally:
                self.cf.CFRelease(search_btn)

            deadline = time.monotonic() + 1.2
            while time.monotonic() < deadline:
                field = _find_search_field()
                if field is not None:
                    break
                time.sleep(0.05)

        if field is None:
            return False

        try:
            self.ax.AXUIElementSetAttributeValue(field, self._attr("AXFocused"), self._k_true)
            cf_q = self._cfstr(target_title)
            try:
                self.ax.AXUIElementSetAttributeValue(field, self._attr("AXValue"), cf_q)
            finally:
                self.cf.CFRelease(cf_q)

            def _close_search() -> None:
                wins_ref = self._copy_attr(app_el, "AXWindows")
                if wins_ref:
                    try:
                        if self.cf.CFGetTypeID(wins_ref) == self.cf.CFArrayGetTypeID():
                            for i in range(int(self.cf.CFArrayGetCount(wins_ref))):
                                w = int(self.cf.CFArrayGetValueAtIndex(wins_ref, i))
                                cb = self._find_element(
                                    w,
                                    lambda el, r: r == "AXButton" and self._get_str(el, "AXIdentifier") == "gemini_search_close_button",
                                    skip_outline=False,
                                    max_depth=10,
                                )
                                if cb is not None:
                                    try:
                                        self.perform_action(cb, "AXPress")
                                    finally:
                                        self.cf.CFRelease(cb)
                                    return
                    finally:
                        self.cf.CFRelease(wins_ref)
                try:
                    self.perform_action(field, "AXCancel")
                except Exception:
                    pass

            match_deadline = time.monotonic() + 1.8
            while time.monotonic() < match_deadline:
                def _is_match(el: int, role: str) -> bool:
                    if role not in ("AXButton", "AXRow", "AXStaticText", "AXMenuItem"):
                        return False
                    ident = self._get_str(el, "AXIdentifier")
                    desc = self._get_str(el, "AXDescription").strip()
                    if ident.startswith("gemini_search_row_") and (
                        desc == target_title
                        or desc.startswith(target_title + "、")
                        or desc.startswith(target_title + ",")
                    ):
                        return True
                    for attr in ("AXTitle", "AXDescription", "AXValue"):
                        val = self._get_str(el, attr).strip()
                        if val and parse_sidebar_row_label(val)["title"] == target_title:
                            return True
                    return False

                hit = None
                wins_ref = self._copy_attr(app_el, "AXWindows")
                if wins_ref:
                    try:
                        if self.cf.CFGetTypeID(wins_ref) == self.cf.CFArrayGetTypeID():
                            for i in range(int(self.cf.CFArrayGetCount(wins_ref))):
                                w = int(self.cf.CFArrayGetValueAtIndex(wins_ref, i))
                                hit = self._find_element(w, _is_match, skip_outline=False, max_depth=14)
                                if hit is not None:
                                    break
                    finally:
                        self.cf.CFRelease(wins_ref)
                if hit is None:
                    hit = self._find_element(win_el, _is_match, skip_outline=False, max_depth=14)
                if hit is not None:
                    try:
                        if self.perform_action(hit, "AXPress") == 0:
                            return True
                    finally:
                        self.cf.CFRelease(hit)
                time.sleep(0.08)

            _close_search()
            return False
        finally:
            self.cf.CFRelease(field)

    def open_conversation(self, pid: int, target_title: str, timeout_s: float = 3.0) -> tuple[bool, str]:
        """Navigate silently to exact Gemini conversation title and verify AXWindow.AXTitle."""
        target = target_title.strip()
        if not target:
            return False, "empty_target_title"

        ok_win, _ = self.ensure_main_window_silent(pid)
        if not ok_win:
            return False, "no_main_window"

        cur = self.get_window_title(pid)
        if cur == target:
            return True, "already_open"

        app_el = self._app_el(pid)
        method = ""
        try:
            win = self._find_main_window(app_el)
            if not win:
                return False, "no_main_window"
            try:
                outline = self._ensure_sidebar_open(pid, win)
                if outline is not None:
                    try:
                        btns = self._iter_sidebar_buttons(outline) or []
                        try:
                            for btn, parsed in btns:
                                if parsed["title"] == target:
                                    if self.perform_action(btn, "AXPress") == 0:
                                        method = "sidebar"
                                    break
                        finally:
                            for btn, _ in btns:
                                self.cf.CFRelease(btn)
                    finally:
                        self.cf.CFRelease(outline)

                if not method:
                    if self._press_menubar_conversation(app_el, target):
                        method = "menubar"

                if not method:
                    if self._search_and_open_conversation(app_el, win, target):
                        method = "search"
            finally:
                self.cf.CFRelease(win)
        finally:
            self.cf.CFRelease(app_el)

        if not method:
            return False, "conversation_not_found"

        deadline = time.monotonic() + max(0.5, timeout_s)
        while time.monotonic() < deadline:
            cur_after = self.get_window_title(pid)
            if cur_after == target:
                return True, method
            time.sleep(0.08)
        return False, "title_verification_failed"

    def _find_composer(self, win_el: int) -> int | None:
        def _is_named(el: int, role: str) -> bool:
            if role != "AXTextArea":
                return False
            desc = self._get_str(el, "AXDescription").lower()
            return any(tok in desc for tok in (
                "问问 gemini", "分配任务", "描述任务", "接下来做什么",
                "ask gemini", "assign a task", "describe a task", "message gemini",
                "what’s next", "what's next",
            ))

        ta = self._find_element(win_el, _is_named, skip_outline=True, max_depth=12)
        if ta is not None:
            return ta
        return self._find_element(win_el, lambda _el, r: r == "AXTextArea", skip_outline=True, max_depth=12)

    def _is_send_button(self, el: int, role: str) -> bool:
        if role != "AXButton":
            return False
        ident = self._get_str(el, "AXIdentifier").strip()
        if ident in ("mic_button", "stc_button", "sidebar_toggle", "sidebar_header_search", "tempChat_button", "collapse_button", "tab_stop_model_selector"):
            return False
        if ident == "send_button":
            return self._get_bool(el, "AXEnabled", True)
        desc = self._get_str(el, "AXDescription").strip().lower()
        title = self._get_str(el, "AXTitle").strip().lower()
        if desc in ("提交", "发送", "submit", "send", "send message") or title in ("提交", "发送", "submit", "send", "send message"):
            return self._get_bool(el, "AXEnabled", True)
        return False

    def _is_stop_element(self, el: int, role: str) -> bool:
        if role != "AXButton":
            return False
        ident = self._get_str(el, "AXIdentifier").strip()
        if ident in ("stop_button", "AGENT_WORK_PANE_STOP_BUTTON"):
            return True
        desc = self._get_str(el, "AXDescription").strip().lower()
        title = self._get_str(el, "AXTitle").strip().lower()
        label = f"{title} {desc}".strip()
        return (
            desc in ("停止", "停止回答", "stop", "stop response", "stop generating")
            or title in ("停止", "停止回答", "stop", "stop response", "stop generating")
            or "停止回答" in label
            or "stop response" in label
            or "stop generating" in label
        )

    def _has_stop_button(self, win_el: int) -> bool:
        stop_el = self._find_element(win_el, self._is_stop_element, skip_outline=True, max_depth=14)
        if stop_el is not None:
            self.cf.CFRelease(stop_el)
            return True
        return False

    def write_prompt_and_submit(self, pid: int, prompt_text: str) -> tuple[bool, str]:
        """Inject prompt directly via AXValue and press submit without touching clipboard or focus."""
        if not prompt_text or not prompt_text.strip():
            return False, "empty_prompt"
        app_el = self._app_el(pid)
        try:
            win = self._find_main_window(app_el)
            if not win:
                return False, "no_main_window"
            try:
                ta = self._find_composer(win)
                if ta is None:
                    return False, "composer_not_found"
                try:
                    self.ax.AXUIElementSetAttributeValue(ta, self._attr("AXFocused"), self._k_true)
                    cf_empty = self._cfstr("")
                    try:
                        self.ax.AXUIElementSetAttributeValue(ta, self._attr("AXValue"), cf_empty)
                    finally:
                        self.cf.CFRelease(cf_empty)
                    cf_prompt = self._cfstr(prompt_text)
                    try:
                        err = int(self.ax.AXUIElementSetAttributeValue(ta, self._attr("AXValue"), cf_prompt))
                    finally:
                        self.cf.CFRelease(cf_prompt)
                    if err != 0:
                        return False, "ax_set_value_failed"
                    actual = self._get_str(ta, "AXValue")
                    expected_prefix = prompt_text[: min(len(prompt_text), 32)]
                    if not actual or not actual.startswith(expected_prefix):
                        return False, "ax_value_verify_failed"
                finally:
                    self.cf.CFRelease(ta)

                # Wait up to 1.5s for send button to become enabled.
                # Distinguish "never attempted AXPress" from "AXPress invoked with uncertain outcome".
                sent = False
                press_attempted = False
                btn_deadline = time.monotonic() + 1.5
                while time.monotonic() < btn_deadline:
                    btn = self._find_element(win, self._is_send_button, skip_outline=True, max_depth=14)
                    if btn is not None:
                        press_attempted = True
                        try:
                            rc_press = int(self.perform_action(btn, "AXPress"))
                        finally:
                            self.cf.CFRelease(btn)
                        if rc_press == 0:
                            sent = True
                            break
                        return False, "submit_press_uncertain"
                    time.sleep(0.05)

                if not sent:
                    return False, ("submit_press_uncertain" if press_attempted else "send_button_not_found_or_disabled")

                # Confirm submission took effect
                verify_deadline = time.monotonic() + 2.0
                while time.monotonic() < verify_deadline:
                    if self._has_stop_button(win):
                        return True, "submitted_stop_visible"
                    ta2 = self._find_composer(win)
                    if ta2 is not None:
                        try:
                            cur_val = self._get_str(ta2, "AXValue").strip()
                            if cur_val in (
                                "", "问问 Gemini", "分配任务", "描述任务", "接下来做什么？",
                                "Ask Gemini", "Assign a task", "Describe a task", "Message Gemini",
                                "What’s next?", "What's next?",
                            ):
                                return True, "submitted_composer_cleared"
                        finally:
                            self.cf.CFRelease(ta2)
                    time.sleep(0.06)
                return False, "submit_unconfirmed"
            finally:
                self.cf.CFRelease(win)
        finally:
            self.cf.CFRelease(app_el)

    def press_new_chat(self, pid: int, timeout_s: float = 2.0) -> bool:
        app_el = self._app_el(pid)
        try:
            win = self._find_main_window(app_el)
            triggered = False
            if win is not None:
                try:
                    btn = self._find_element(
                        win,
                        lambda el, r: (
                            r == "AXButton"
                            and (
                                self._get_str(el, "AXDescription").strip() in ("发起新对话", "New chat", "New Chat")
                                or self._get_str(el, "AXTitle").strip() in ("发起新对话", "New chat", "New Chat")
                                or self._get_str(el, "AXIdentifier").strip().lower() in ("new-chat", "new_chat", "actions_bar_new_chat")
                            )
                        ),
                        skip_outline=False,
                        max_depth=10,
                    )
                    if btn is not None:
                        try:
                            if not self._get_bool(btn, "AXEnabled", True):
                                return True
                            triggered = self.perform_action(btn, "AXPress") == 0
                        finally:
                            self.cf.CFRelease(btn)
                finally:
                    self.cf.CFRelease(win)

            if not triggered:
                mb = self._copy_attr(app_el, "AXMenuBar")
                if mb:
                    try:
                        item = self._find_element(
                            mb,
                            lambda el, r: r == "AXMenuItem" and self._get_str(el, "AXTitle").strip() in ("发起新对话", "New chat", "New Chat"),
                            skip_outline=False,
                            max_depth=4,
                        )
                        if item is not None:
                            try:
                                if not self._get_bool(item, "AXEnabled", True):
                                    return True
                                triggered = self.perform_action(item, "AXPress") == 0
                            finally:
                                self.cf.CFRelease(item)
                    finally:
                        self.cf.CFRelease(mb)
            if not triggered:
                return False
        finally:
            self.cf.CFRelease(app_el)

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            title = self.get_window_title(pid)
            if title in NEW_CHAT_TITLES:
                return True
            time.sleep(0.06)
        return self.get_window_title(pid) in NEW_CHAT_TITLES

    def ensure_task_mode(self, pid: int, timeout_s: float = 1.5) -> bool:
        app_el = self._app_el(pid)
        try:
            win = self._find_main_window(app_el)
            if not win:
                return False
            try:
                def _is_task_active(w: int) -> bool:
                    btn = self._find_element(
                        w,
                        lambda el, r: r == "AXButton" and self._get_str(el, "AXIdentifier") == "chat-input-mode-assign",
                        skip_outline=True,
                        max_depth=12,
                    )
                    if btn is not None:
                        try:
                            if self._get_bool(btn, "AXSelected", False):
                                return True
                        finally:
                            self.cf.CFRelease(btn)
                    ta = self._find_composer(w)
                    if ta is not None:
                        try:
                            d = self._get_str(ta, "AXDescription").lower()
                            if any(tok in d for tok in ("分配任务", "描述任务", "assign a task", "describe a task")):
                                return True
                        finally:
                            self.cf.CFRelease(ta)
                    return False

                if _is_task_active(win):
                    return True

                btn = self._find_element(
                    win,
                    lambda el, r: (
                        r == "AXButton"
                        and (
                            self._get_str(el, "AXIdentifier") == "chat-input-mode-assign"
                            or self._get_str(el, "AXDescription").strip() in ("任务", "Task")
                        )
                    ),
                    skip_outline=True,
                    max_depth=12,
                )
                if btn is None:
                    return False
                try:
                    if self.perform_action(btn, "AXPress") != 0:
                        return False
                finally:
                    self.cf.CFRelease(btn)

                deadline = time.monotonic() + timeout_s
                while time.monotonic() < deadline:
                    if _is_task_active(win):
                        return True
                    time.sleep(0.05)
                return _is_task_active(win)
            finally:
                self.cf.CFRelease(win)
        finally:
            self.cf.CFRelease(app_el)

    def _has_folder_chip(self, win_el: int, target_dir: str, target_name: str) -> bool:
        target_res = Path(target_dir).resolve()
        matched = False

        def _visit(el: int, role: str) -> bool:
            nonlocal matched
            ident = self._get_str(el, "AXIdentifier")
            desc = self._get_str(el, "AXDescription").strip()
            title = self._get_str(el, "AXTitle").strip()
            if role == "AXButton" and desc.lower() in ("folders", "文件夹") and title.lower() in ("", "folders", "文件夹"):
                return False
            if "folder" in ident.lower() and "chip" in ident.lower():
                for txt in self._collect_texts(el, max_depth=3):
                    if txt in (target_dir, target_name):
                        matched = True
                        return True
                    try:
                        p = Path(txt).expanduser()
                        if p.is_absolute() and p.resolve() == target_res:
                            matched = True
                            return True
                    except OSError:
                        pass
            if role in ("AXButton", "AXGroup"):
                for cand in (self._get_str(el, "AXValue").strip(), title, desc):
                    if cand in (target_dir, target_name, f"Remove {target_name}", f"移除 {target_name}", f"Connected folder: {target_name}"):
                        matched = True
                        return True
            return False

        self._walk(win_el, _visit, skip_outline=True, max_depth=12)
        return matched

    def _find_folders_button(self, win: int) -> int | None:
        return self._find_element(
            win,
            lambda el, r: r == "AXButton" and (
                self._get_str(el, "AXDescription").strip() in ("文件夹", "Folders")
                or self._get_str(el, "AXTitle").strip() in ("文件夹", "Folders")
            ),
            skip_outline=True,
            max_depth=12,
        )

    def _open_folders_menu_items(self, win: int, folders_btn: int) -> tuple[int | None, list[int]]:
        """Open Folders popup menu on daemon thread and return (menu_el, retained_leaf_items)."""
        def _open_menu() -> None:
            try:
                self.perform_action(folders_btn, "AXPress")
            except Exception:
                pass
            finally:
                self.cf.CFRelease(folders_btn)

        self.cf.CFRetain(folders_btn)
        threading.Thread(target=_open_menu, daemon=True).start()

        menu_el = None
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            menu_el = self._find_element(folders_btn, lambda _el, r: r == "AXMenu", skip_outline=False, max_depth=4)
            if menu_el is None:
                menu_el = self._find_element(win, lambda _el, r: r == "AXMenu", skip_outline=True, max_depth=10)
            if menu_el is not None:
                break
            time.sleep(0.05)
        if menu_el is None:
            return None, []

        items: list[int] = []
        raw_items: list[int] = []
        self._walk(
            menu_el,
            lambda el, r: (self.cf.CFRetain(el), raw_items.append(el), False)[2] if r == "AXMenuItem" else False,
            skip_outline=False,
            max_depth=8,
        )
        for item in raw_items:
            if self._has_descendant_role(item, "AXMenuItem", max_depth=3):
                self.cf.CFRelease(item)
            else:
                items.append(item)
        return menu_el, items

    def list_bookmarked_folders(self, pid: int) -> list[str]:
        """Read-only inspect visible bookmarked folders when Folders button is exposed (no state mutation)."""
        app_el = self._app_el(pid)
        try:
            win = self._find_main_window(app_el)
            if not win:
                return []
            try:
                folders_btn = self._find_folders_button(win)
                if folders_btn is None:
                    return []
                try:
                    menu_el, items = self._open_folders_menu_items(win, folders_btn)
                    if menu_el is None:
                        return []
                    found: list[str] = []
                    try:
                        for item in items:
                            for cand in self._collect_texts(item, max_depth=2):
                                if cand and cand not in ("添加新文件夹…", "Add new folder…", "Add new folder...") and cand not in found:
                                    found.append(cand)
                        try:
                            self.perform_action(menu_el, "AXCancel")
                        except Exception:
                            pass
                        return found
                    finally:
                        for item in items:
                            self.cf.CFRelease(item)
                        self.cf.CFRelease(menu_el)
                finally:
                    self.cf.CFRelease(folders_btn)
            finally:
                self.cf.CFRelease(win)
        finally:
            self.cf.CFRelease(app_el)

    def attach_workspace_folder(self, pid: int, workspace_path: str) -> tuple[bool, str]:
        target_res = Path(workspace_path).expanduser().resolve()
        target_dir = str(target_res)
        target_name = target_res.name
        app_el = self._app_el(pid)
        try:
            win = self._find_main_window(app_el)
            if not win:
                return False, "no_main_window"
            try:
                if self._has_folder_chip(win, target_dir, target_name):
                    return True, "already_attached"

                folders_btn = self._find_folders_button(win)
                if folders_btn is None:
                    # Gemini attaches default folder (e.g. Documents) with a '移除'/'Remove' chip button.
                    remove_btn = self._find_element(
                        win,
                        lambda el, r: r == "AXButton" and (
                            self._get_str(el, "AXDescription").strip() in ("移除", "Remove")
                            or self._get_str(el, "AXTitle").strip() in ("移除", "Remove")
                        ),
                        skip_outline=True,
                        max_depth=12,
                    )
                    if remove_btn is not None:
                        try:
                            self.perform_action(remove_btn, "AXPress")
                        finally:
                            self.cf.CFRelease(remove_btn)
                        wait_btn = time.monotonic() + 1.5
                        while time.monotonic() < wait_btn:
                            folders_btn = self._find_folders_button(win)
                            if folders_btn is not None:
                                break
                            time.sleep(0.05)

                if folders_btn is None:
                    return False, "folders_button_not_found"

                try:
                    menu_el, items = self._open_folders_menu_items(win, folders_btn)
                    if menu_el is None:
                        return False, "folders_menu_did_not_open"
                    try:
                        matched_item: int | None = None
                        name_matches: list[int] = []
                        available: list[str] = []
                        for item in items:
                            texts = self._collect_texts(item, max_depth=2)
                            for cand in texts:
                                if cand and cand not in available:
                                    available.append(cand)
                                try:
                                    p = Path(cand).expanduser()
                                    if p.is_absolute() and p.resolve() == target_res:
                                        matched_item = item
                                        break
                                except OSError:
                                    pass
                                if cand == target_dir:
                                    matched_item = item
                                    break
                                if cand == target_name:
                                    name_matches.append(item)
                            if matched_item is not None:
                                break

                        if matched_item is None and len(name_matches) == 1:
                            matched_item = name_matches[0]

                        if matched_item is not None:
                            if self.perform_action(matched_item, "AXPress") != 0:
                                return False, "folder_item_press_failed"
                            chip_deadline = time.monotonic() + 1.5
                            while time.monotonic() < chip_deadline:
                                if self._has_folder_chip(win, target_dir, target_name):
                                    return True, "attached"
                                time.sleep(0.05)
                            return True, "attached_unverified"

                        try:
                            self.perform_action(menu_el, "AXCancel")
                        except Exception:
                            pass
                        return False, "workspace_not_bookmarked_in_gemini:" + ",".join(available[:10])
                    finally:
                        for item in items:
                            self.cf.CFRelease(item)
                        self.cf.CFRelease(menu_el)
                finally:
                    self.cf.CFRelease(folders_btn)
            finally:
                self.cf.CFRelease(win)
        finally:
            self.cf.CFRelease(app_el)

    def rename_conversation(self, pid: int, current_title: str, new_title: str) -> tuple[bool, str]:
        """Attempt to rename a Gemini conversation via sidebar context menu."""
        if current_title == new_title:
            return True, current_title
        app_el = self._app_el(pid)
        try:
            win = self._find_main_window(app_el)
            if not win:
                return False, current_title
            try:
                outline = self._ensure_sidebar_open(pid, win)
                if outline is None:
                    return False, current_title
                target_btn = None
                try:
                    btns = self._iter_sidebar_buttons(outline) or []
                    try:
                        for btn, parsed in btns:
                            if parsed["title"] == current_title:
                                self.cf.CFRetain(btn)
                                target_btn = btn
                                break
                    finally:
                        for btn, _ in btns:
                            self.cf.CFRelease(btn)
                finally:
                    self.cf.CFRelease(outline)

                if target_btn is None:
                    return False, current_title

                def _open_ctx() -> None:
                    try:
                        self.perform_action(target_btn, "AXShowMenu")
                    except Exception:
                        pass
                    finally:
                        self.cf.CFRelease(target_btn)

                self.cf.CFRetain(target_btn)
                try:
                    threading.Thread(target=_open_ctx, daemon=True).start()
                    rename_item = None
                    menu_el = None
                    deadline = time.monotonic() + 1.5
                    while time.monotonic() < deadline:
                        menu_el = self._find_element(
                            target_btn,
                            lambda _el, r: r == "AXMenu",
                            skip_outline=False,
                            max_depth=4,
                        )
                        if menu_el is None:
                            menu_el = self._find_element(win, lambda _el, r: r == "AXMenu", skip_outline=False, max_depth=10)
                        if menu_el is not None:
                            rename_item = self._find_element(
                                menu_el,
                                lambda el, r: r == "AXMenuItem" and self._get_str(el, "AXTitle").strip().lower() in ("重命名", "rename", "重命名…", "rename…"),
                                skip_outline=False,
                                max_depth=4,
                            )
                            if rename_item is not None:
                                break
                            self.cf.CFRelease(menu_el)
                            menu_el = None
                        time.sleep(0.05)

                    if rename_item is None:
                        if menu_el is not None:
                            try:
                                self.perform_action(menu_el, "AXCancel")
                            except Exception:
                                pass
                            self.cf.CFRelease(menu_el)
                        return False, current_title

                    try:
                        if self.perform_action(rename_item, "AXPress") != 0:
                            return False, current_title
                    finally:
                        self.cf.CFRelease(rename_item)
                        if menu_el is not None:
                            self.cf.CFRelease(menu_el)
                finally:
                    self.cf.CFRelease(target_btn)

                # Wait for rename sheet/dialog and submit new title
                dlg_deadline = time.monotonic() + 1.5
                renamed = False
                while time.monotonic() < dlg_deadline:
                    # Check AXSheets on win or any AXDialog window
                    containers: list[int] = []
                    sheets = self._copy_attr(win, "AXSheets")
                    if sheets and self.cf.CFGetTypeID(sheets) == self.cf.CFArrayGetTypeID():
                        for i in range(int(self.cf.CFArrayGetCount(sheets))):
                            s = int(self.cf.CFArrayGetValueAtIndex(sheets, i))
                            self.cf.CFRetain(s)
                            containers.append(s)
                    if sheets:
                        self.cf.CFRelease(sheets)
                    wins_ref = self._copy_attr(app_el, "AXWindows")
                    if wins_ref and self.cf.CFGetTypeID(wins_ref) == self.cf.CFArrayGetTypeID():
                        for i in range(int(self.cf.CFArrayGetCount(wins_ref))):
                            w = int(self.cf.CFArrayGetValueAtIndex(wins_ref, i))
                            if w != win and self._get_str(w, "AXTitle").strip() in ("重命名此对话", "重命名此任务", "Rename this chat", "Rename this task"):
                                self.cf.CFRetain(w)
                                containers.append(w)
                    if wins_ref:
                        self.cf.CFRelease(wins_ref)

                    # Also check if inline textfield appeared inside sidebar
                    if not containers:
                        containers.append(win)
                        self.cf.CFRetain(win)

                    for c in containers:
                        try:
                            tf = self._find_element(
                                c,
                                lambda el, r: r == "AXTextField" and self._get_str(el, "AXIdentifier") not in ("settings_search_field", "gemini_search_text_field"),
                                skip_outline=(c == win and len(containers) == 1),
                                max_depth=14,
                            )
                            if tf is not None:
                                try:
                                    self.ax.AXUIElementSetAttributeValue(tf, self._attr("AXFocused"), self._k_true)
                                    cf_t = self._cfstr(new_title)
                                    try:
                                        self.ax.AXUIElementSetAttributeValue(tf, self._attr("AXValue"), cf_t)
                                    finally:
                                        self.cf.CFRelease(cf_t)
                                finally:
                                    self.cf.CFRelease(tf)
                                ok_btn = self._find_element(
                                    c,
                                    lambda el, r: (
                                        r == "AXButton"
                                        and (
                                            self._get_str(el, "AXIdentifier").strip() == "action-button-1"
                                            or self._get_str(el, "AXTitle").strip() in ("确认", "确定", "保存", "Confirm", "OK", "Save")
                                            or self._get_str(el, "AXDescription").strip() in ("确认", "确定", "保存", "Confirm", "OK", "Save")
                                        )
                                    ),
                                    skip_outline=False,
                                    max_depth=14,
                                )
                                if ok_btn is not None:
                                    try:
                                        self.perform_action(ok_btn, "AXPress")
                                        renamed = True
                                    finally:
                                        self.cf.CFRelease(ok_btn)
                                break
                        finally:
                            self.cf.CFRelease(c)
                    if renamed:
                        break
                    time.sleep(0.08)

                if renamed:
                    wait_end = time.monotonic() + 6.0
                    while time.monotonic() < wait_end:
                        cur = self.get_window_title(pid)
                        if cur == new_title:
                            return True, new_title
                        time.sleep(0.12)
                actual = self.get_window_title(pid) or current_title
                return actual == new_title, actual
            finally:
                self.cf.CFRelease(win)
        finally:
            self.cf.CFRelease(app_el)

    def poll_conversation(self, pid: int) -> dict[str, Any]:
        app_el = self._app_el(pid)
        try:
            win = self._find_main_window(app_el)
            if not win:
                return {
                    "has_window": False,
                    "window_title": None,
                    "busy": False,
                    "confirmation_card": None,
                    "has_copy_button": False,
                    "chunks": [],
                    "text": "",
                }
            win_title = normalize_window_title(self._get_str(win, "AXTitle"))
            busy = False
            has_copy = False
            confirmation_card: str | None = None
            chunks: list[str] = []

            def _visit(el: int, role: str) -> bool:
                nonlocal busy, has_copy, confirmation_card
                if role in ("AXTextArea", "AXTextField", "AXSearchField", "AXMenuBar", "AXToolbar"):
                    return False
                ident = self._get_str(el, "AXIdentifier")
                desc = self._get_str(el, "AXDescription")
                title = self._get_str(el, "AXTitle")

                if ident:
                    if ident in ("stop_button", "AGENT_WORK_PANE_STOP_BUTTON"):
                        busy = True
                    for cid in CONFIRMATION_CARD_IDS:
                        if cid in ident:
                            confirmation_card = cid
                            break

                if self._is_stop_element(el, role):
                    busy = True
                    return False

                extract_text = False
                if role == "AXButton":
                    desc_l = desc.strip().lower()
                    title_l = title.strip().lower()
                    if ident == "copy-button" or desc_l in ("拷贝", "copy", "copy response") or title_l in ("拷贝", "copy", "copy response"):
                        has_copy = True
                    elif ident == "copy-button-copy-button":
                        extract_text = True
                elif role in ("AXHeading", "AXStaticText", "AXWebArea"):
                    extract_text = True
                elif role == "AXGroup":
                    val = self._get_str(el, "AXValue")
                    if val and ("Generating your response" in val or "正在生成回答" in val):
                        busy = True

                if extract_text:
                    ax_val = self._get_str(el, "AXValue")
                    if "[[T3_BRIDGE_" in desc or (desc and len(desc) > len(ax_val)):
                        val = desc
                    else:
                        val = ax_val or desc or title
                    val_s = (val or "").strip()
                    if (
                        val_s
                        and val_s not in CHROME_TEXTS
                        and not val_s.endswith("，想聊点什么？")
                        and not val_s.startswith("Hi ")
                        and "what’s on your mind?" not in val_s
                        and "what's on your mind?" not in val_s
                    ):
                        if "Generating your response" in val_s or "正在生成回答" in val_s:
                            busy = True
                        if not chunks or chunks[-1] != val_s:
                            chunks.append(val_s)
                return False

            try:
                self._walk(win, _visit, skip_outline=True)
            finally:
                self.cf.CFRelease(win)

            return {
                "has_window": True,
                "window_title": win_title,
                "busy": busy,
                "confirmation_card": confirmation_card,
                "has_copy_button": has_copy,
                "chunks": chunks,
                "text": "\n".join(chunks),
            }
        finally:
            self.cf.CFRelease(app_el)

    def create_task_conversation(
        self,
        pid: int,
        target_title: str,
        init_prompt: str,
        workspace_path: str | None = None,
        wait_s: float = 35.0,
    ) -> tuple[bool, str, str]:
        """Create a fresh Gemini Task conversation, bind optional workspace, send init prompt, and return actual title."""
        ok_win, _ = self.ensure_main_window_silent(pid)
        if not ok_win:
            return False, "no_main_window", ""
        if not self.press_new_chat(pid):
            return False, "new_chat_failed", ""
        if not self.ensure_task_mode(pid):
            return False, "switch_task_mode_failed", ""
        if workspace_path:
            ok_ws, ws_reason = self.attach_workspace_folder(pid, workspace_path)
            if not ok_ws:
                return False, ws_reason, ""

        watcher = DiagnosticLogWatcher()
        watcher.snapshot()
        ok_sub, sub_reason = self.write_prompt_and_submit(pid, init_prompt)
        if not ok_sub:
            return False, sub_reason, ""

        # Wait for Gemini reply and automatic title generation
        deadline = time.monotonic() + max(10.0, wait_s)
        saw_start = False
        saw_done = False
        settled_since: float | None = None
        actual_title = ""
        last_reply = ""
        while time.monotonic() < deadline:
            s_start, s_done = watcher.read_updates()
            saw_start = saw_start or s_start
            if s_done:
                saw_done = True
            poll = self.poll_conversation(pid)
            if poll.get("confirmation_card"):
                return False, "init_requires_confirmation", str(poll.get("text") or "")
            actual_title = poll.get("window_title") or ""
            last_reply = str(poll.get("text") or "")
            busy = bool(poll.get("busy"))
            if not busy and (saw_done or poll.get("has_copy_button")) and actual_title not in NEW_CHAT_TITLES:
                if settled_since is None:
                    settled_since = time.monotonic()
                elif time.monotonic() - settled_since >= 1.0:
                    break
            else:
                settled_since = None
            time.sleep(0.3)

        if not actual_title or actual_title in NEW_CHAT_TITLES:
            actual_title = self.get_window_title(pid) or ""
        if not actual_title or actual_title in NEW_CHAT_TITLES:
            return False, "auto_title_not_generated", last_reply

        ok_ren, final_title = self.rename_conversation(pid, actual_title, target_title)
        return True, final_title, ("renamed" if ok_ren else "auto_titled")


class DiagnosticLogWatcher:
    """Incrementally tails Gemini diagnostic*.log files across rotations."""

    def __init__(self, log_dir: Path | None = None) -> None:
        self.log_dir = log_dir or (Path.home() / "Library/Caches/com.google.GeminiMacOS/Logs")
        self.current_log: Path | None = None
        self.offset: int = 0
        self.tail_buf: str = ""
        self.saw_start: bool = False
        self.saw_recent_activity: bool = False

    def _newest_log(self) -> Path | None:
        if not self.log_dir.is_dir():
            return None
        candidates: list[tuple[int, str, Path]] = []
        for entry in self.log_dir.glob("diagnostic*.log"):
            try:
                st = entry.stat()
            except OSError:
                continue
            if entry.is_file():
                candidates.append((st.st_mtime_ns, entry.name, entry))
        if not candidates:
            return None
        candidates.sort(key=lambda x: (x[0], x[1]))
        return candidates[-1][2]

    def snapshot(self) -> None:
        newest = self._newest_log()
        self.current_log = newest
        self.offset = 0
        self.tail_buf = ""
        self.saw_start = False
        self.saw_recent_activity = False
        if newest is not None:
            try:
                self.offset = newest.stat().st_size
            except OSError:
                self.offset = 0

    def read_updates(self) -> tuple[bool, bool]:
        if not self.log_dir.is_dir():
            return False, False
        newest = self._newest_log()
        chunks: list[str] = []
        if self.current_log is not None and newest is not None and newest != self.current_log:
            try:
                sz = self.current_log.stat().st_size
                if sz > self.offset:
                    with self.current_log.open("rb") as fh:
                        fh.seek(self.offset)
                        chunks.append(fh.read(sz - self.offset).decode("utf-8", errors="replace"))
            except OSError:
                pass
            self.current_log = newest
            self.offset = 0
        elif self.current_log is None and newest is not None:
            self.current_log = newest
            self.offset = 0

        if self.current_log is not None:
            try:
                sz = self.current_log.stat().st_size
                if sz < self.offset:
                    self.offset = 0
                    self.tail_buf = ""
                if sz > self.offset:
                    with self.current_log.open("rb") as fh:
                        fh.seek(self.offset)
                        raw = fh.read(sz - self.offset)
                    self.offset += len(raw)
                    chunks.append(raw.decode("utf-8", errors="replace"))
            except OSError:
                pass

        if not chunks:
            self.saw_recent_activity = False
            return self.saw_start, False

        combined = self.tail_buf + "".join(chunks)
        for m in DIAGNOSTIC_START_MARKERS:
            if m in combined:
                self.saw_start = True
                break
        last_done = max((combined.rfind(m) for m in DIAGNOSTIC_DONE_MARKERS), default=-1)
        last_act = max((combined.rfind(m) for m in DIAGNOSTIC_ACTIVITY_MARKERS), default=-1)
        saw_done = last_done != -1 and last_done > last_act
        self.saw_recent_activity = last_act > last_done
        if saw_done or combined.endswith("\n"):
            self.tail_buf = ""
        else:
            nl = combined.rfind("\n")
            self.tail_buf = (combined[nl + 1:] if nl != -1 else combined)[-256:]
        return self.saw_start, saw_done
