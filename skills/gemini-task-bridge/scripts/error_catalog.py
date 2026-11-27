"""Bilingual (zh/en) actionable error catalog for gemini-task-bridge."""
from __future__ import annotations

import os

CATALOG: dict[str, dict[str, tuple[str, str]]] = {
    "ax_not_trusted": {
        "zh": (
            "后台服务尚未获得 macOS 辅助功能权限。",
            "打开「系统设置 → 隐私与安全性 → 辅助功能」，启用 `gemini-task-bridge doctor` 显示的 Python.app，然后运行 `gemini-task-bridge service restart`。",
        ),
        "en": (
            "The background daemon does not have macOS Accessibility permission.",
            "Open System Settings -> Privacy & Security -> Accessibility, enable the Python.app shown by `gemini-task-bridge doctor`, then run `gemini-task-bridge service restart`.",
        ),
    },
    "gemini_not_running": {
        "zh": (
            "Gemini Desktop 未在运行。",
            "运行 `open -a Gemini` 启动 Gemini.app 并保持主窗口打开。",
        ),
        "en": (
            "Gemini Desktop is not running.",
            "Run `open -a Gemini` to launch Gemini.app and keep its main window open.",
        ),
    },
    "screen_locked": {
        "zh": (
            "macOS 屏幕已锁定，无法操作 Gemini 窗口。",
            "解锁屏幕（或唤醒显示器）后重试；可运行 `gemini-task-bridge doctor` 检查状态。",
        ),
        "en": (
            "The macOS screen is locked, so Gemini windows cannot be controlled.",
            "Unlock the screen (or wake the display) and retry; run `gemini-task-bridge doctor` to check status.",
        ),
    },
    "workspace_not_bookmarked_in_gemini": {
        "zh": (
            "工作区目录尚未收藏到 Gemini 的 Folders 列表。",
            "先在 Gemini.app 中将目标工作区目录添加/收藏到 Folders，再重试。",
        ),
        "en": (
            "The workspace directory is not bookmarked in Gemini's Folders menu.",
            "Bookmark or add the target workspace directory in Gemini.app's Folders menu first, then retry.",
        ),
    },
    "duplicate_gemini_title_in_pairs": {
        "zh": (
            "该 Gemini 对话标题已被另一个已启用的配对占用。",
            "换一个新的 Gemini 对话标题，或运行 `gemini-task-bridge pair list --json` 后移除冲突配对。",
        ),
        "en": (
            "This Gemini conversation title is already bound to another enabled pair.",
            "Use a new Gemini chat title, or run `gemini-task-bridge pair list --json` and remove the conflicting pair.",
        ),
    },
    "pair_has_active_task": {
        "zh": (
            "该配对仍有未完成的进行中任务，暂不能修改或重建。",
            "运行 `gemini-task-bridge status --json` 等待当前任务结束，或改用新的 pairId。",
        ),
        "en": (
            "This pair has an active in-flight task and cannot be modified or recreated right now.",
            "Check `gemini-task-bridge status --json` and wait for the active task to finish, or use a new pairId.",
        ),
    },
    "unknown_pair_id": {
        "zh": (
            "找不到指定的配对 ID。",
            "运行 `gemini-task-bridge pair list --json` 查看已有配对，或先创建新配对。",
        ),
        "en": (
            "The specified pair ID does not exist.",
            "Run `gemini-task-bridge pair list --json` to view existing pairs, or create a new pair first.",
        ),
    },
    "daemon_upgrade_required_for_v21_features": {
        "zh": (
            "后台服务版本过旧或运行状态异常，不支持当前功能。",
            "运行 `gemini-task-bridge service restart`（若刚更新脚本，请先运行 `install.sh` 再重启服务）。",
        ),
        "en": (
            "The background daemon is outdated or unhealthy and cannot handle this feature.",
            "Run `gemini-task-bridge service restart` (or re-run `install.sh` to update scripts first, then restart).",
        ),
    },
    "control_plane_unavailable": {
        "zh": (
            "后台控制面当前不可用（服务未运行、健康状态过期或未获辅助功能授权）。",
            "运行 `gemini-task-bridge doctor` 检查是哪一环失败，并按提示启动服务或授予辅助功能权限。",
        ),
        "en": (
            "The daemon control plane is unavailable (daemon not running, health stale, or Accessibility missing).",
            "Run `gemini-task-bridge doctor` to inspect the failing stage and start the daemon or grant Accessibility.",
        ),
    },
    "control_request_unconfirmed": {
        "zh": (
            "后台在创建 Gemini 对话期间中断，无法确认对话是否已成功创建。",
            "打开 Gemini.app 侧栏确认对话是否已出现；若已出现，运行 `gemini-task-bridge pair add` 手动绑定。",
        ),
        "en": (
            "The daemon was interrupted while creating the Gemini chat, so its state is unconfirmed.",
            "Check the Gemini.app sidebar to see if the chat was created; if so, bind it manually with `gemini-task-bridge pair add`.",
        ),
    },
    "pair_limit_reached": {
        "zh": (
            "当前 MCP scope 下已启用的配对数量已达上限（10 个）。",
            "运行 `gemini-task-bridge pair remove <pair_id>` 清理不用的配对，或改用新的 `--scope`。",
        ),
        "en": (
            "This MCP scope has reached the limit of 10 enabled pairs.",
            "Run `gemini-task-bridge pair remove <pair_id>` to clean up unused pairs, or use a different `--scope`.",
        ),
    },
    "invalid_mcp_scope": {
        "zh": (
            "MCP scope 名称格式无效。",
            "将 `--scope` 或 `GEMINI_TASK_BRIDGE_MCP_SCOPE` 设为小写字母/数字开头、仅含 `a-z0-9_-` 且不超过 32 字符的名称（如 `codex`）。",
        ),
        "en": (
            "The MCP scope name is invalid.",
            "Set `--scope` or `GEMINI_TASK_BRIDGE_MCP_SCOPE` to match `^[a-z0-9][a-z0-9_-]{0,31}$` (for example `codex`).",
        ),
    },
    "conflicting_launchagent_for_root": {
        "zh": (
            "已有另一个不同 Label 的 LaunchAgent 指向同一桥接目录，已阻止重复安装。",
            "运行 `launchctl bootout gui/$(id -u)/<冲突Label>` 并删除旧 `~/Library/LaunchAgents/<冲突Label>.plist`，或设置 `GEMINI_TASK_BRIDGE_LABEL=<冲突Label>` 复用旧服务。",
        ),
        "en": (
            "Another LaunchAgent with a different Label already points to this bridge root.",
            "Run `launchctl bootout gui/$(id -u)/<conflicting-label>` and remove `~/Library/LaunchAgents/<conflicting-label>.plist`, or set `GEMINI_TASK_BRIDGE_LABEL=<conflicting-label>` to reuse it.",
        ),
    },
    "invalid_idempotency_key": {
        "zh": (
            "幂等键（idempotency_key）格式无效。",
            "请仅使用字母、数字或 `._:-`（长度 1–128），或省略该参数由服务端自动生成。",
        ),
        "en": (
            "The idempotency_key format is invalid.",
            "Use only letters, digits, or `._:-` (length 1-128), or omit it to let the server generate one.",
        ),
    },
    "gemini_init_failed": {
        "zh": (
            "在 Gemini.app 中初始化新对话失败。",
            "确认 Gemini.app 已打开且未锁屏（可运行 `gemini-task-bridge doctor` 检查），处理括号内的具体原因后重试。",
        ),
        "en": (
            "Failed to initialize a new conversation in Gemini.app.",
            "Ensure Gemini.app is open and the screen is unlocked (check with `gemini-task-bridge doctor`), fix the reported detail, and retry.",
        ),
    },
}


def detect_lang(lang: str | None = None) -> str:
    """Resolve UI message language ('zh' or 'en') from explicit arg or environment."""
    if lang is not None and lang.strip():
        return "zh" if lang.strip().lower().startswith("zh") else "en"
    env_lang = os.environ.get("GEMINI_TASK_BRIDGE_LANG", "").strip()
    if env_lang:
        return "zh" if env_lang.lower().startswith("zh") else "en"
    locale_lang = (os.environ.get("LANG") or os.environ.get("LC_ALL") or "").strip()
    return "zh" if locale_lang.lower().startswith("zh") else "en"


def describe(code: str, lang: str | None = None) -> dict[str, str]:
    """Return human-readable bilingual message and concrete fix for an error code."""
    eff = detect_lang(lang)
    raw = str(code or "")
    entry = CATALOG.get(raw)
    if entry is not None:
        msg, fix = entry[eff]
        return {"message": msg, "fix": fix}
    if ":" in raw:
        prefix, _, detail = raw.partition(":")
        entry = CATALOG.get(prefix)
        if entry is not None:
            msg, fix = entry[eff]
            if detail:
                msg = f"{msg} ({detail})"
            return {"message": msg, "fix": fix}
    if eff == "zh":
        return {
            "message": f"操作失败：{raw}" if raw else "发生未知错误。",
            "fix": "运行 `gemini-task-bridge doctor` 检查后台服务与权限状态。",
        }
    return {
        "message": f"Operation failed: {raw}" if raw else "An unknown error occurred.",
        "fix": "Run `gemini-task-bridge doctor` to check daemon status and permissions.",
    }
