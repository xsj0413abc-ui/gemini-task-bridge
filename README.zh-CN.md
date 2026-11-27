# gemini-task-bridge

[English](README.md) | **简体中文**

通过本地 stdio MCP Server 和 CLI，把 **Claude Code**、**Codex**、**Antigravity** 和 **T3 Code** 直接接到 **Google Gemini 桌面端（`Gemini.app`）Task 模式**，让你的编程 AI 能自动新建专属 Gemini 对话、派发任务并读回结果。**仅支持 macOS**（零第三方 Python 依赖，需要 Python 3.9+ 和 `Gemini.app`）。**非 Google 官方工具**，与 Google 无任何关联或背书。

> **验证状态：** 离线单元测试与 stdio MCP 回归测试覆盖 Python 3.9（纯标准库）和 Python 3.12。Codex、Claude Code、Antigravity 的真实宿主端到端验收清单见 [mcp-and-multi-agent.md](skills/gemini-task-bridge/references/mcp-and-multi-agent.md#4-two-stage-acceptance-checklist)。

---

## 3 分钟上手（macOS）

只有后台守护进程（LaunchAgent）需要 macOS 辅助功能权限——**Claude Code、Codex、Antigravity 和终端都不需要辅助功能权限**。

> **最省事的用法：** 装好技能后，直接对你的 AI（Claude Code、Codex 或 Antigravity）说「帮我安装并配置 gemini-task-bridge」。它会按 `SKILL.md` 替你执行下面每一步，你只需要在「系统设置 → 隐私与安全性 → 辅助功能」里打开 `Python.app` 的开关。

### 1. 安装桥接脚本与命令行

```bash
git clone https://github.com/xsj0413abc-ui/gemini-task-bridge.git
cd gemini-task-bridge
bash skills/gemini-task-bridge/scripts/install.sh
```

如果 `~/.local/bin` 不在 `PATH` 中，将其加入 `~/.zshrc` 并重新加载终端：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### 2. 给 `Python.app` 开启辅助功能权限

`install.sh` 会打印解析出的单硬链接 `TCC Python` 路径（`.../Python.app/Contents/MacOS/Python`）：

1. 打开 **系统设置 → 隐私与安全性 → 辅助功能**，添加并启用该 `Python.app`（不要选 `/usr/bin/python3`）。
2. 如果你的工作目录在 `~/Desktop`、`~/Documents` 或 `~/Downloads`，在 **完全磁盘访问权限** 中一并启用该 `Python.app`。

### 3. 安装并体检后台服务

```bash
gemini-task-bridge service install
gemini-task-bridge doctor
```

`doctor` 会按 `daemon → daemon_accessibility → screen → gemini` 顺序逐项检查，若未就绪会直接告诉你卡在哪一步和下一条命令。确认 **Gemini.app** 已打开且全部通过（`doctor --json` 中 `ready: true`）。

### 4. 在宿主中注册 MCP（以及可选的 Skill）

注册 stdio MCP 时带上 `--scope <宿主名>`，每个宿主只看到自己创建的配对，且各自拥有最多 10 个 AI 自动建对话的配额：

- **Claude Code：**
  ```bash
  claude mcp add --transport stdio --scope user gemini-task-bridge -- \
    "$HOME/.local/bin/gemini-task-bridge" mcp --scope claude-code
  ```
- **Codex：**
  ```bash
  codex mcp add gemini-task-bridge -- \
    "$HOME/.local/bin/gemini-task-bridge" mcp --scope codex
  ```
- **Antigravity（`~/.gemini/config/mcp_config.json`）：**
  把 `<ABS_PATH_TO_BIN>` 替换为 `dirname "$(command -v gemini-task-bridge)"` 的实际绝对路径（JSON 里不要写未展开的 `$HOME`）：
  ```json
  {
    "mcpServers": {
      "gemini-task-bridge": {
        "command": "<ABS_PATH_TO_BIN>/gemini-task-bridge",
        "args": ["mcp", "--scope", "antigravity"]
      }
    }
  }
  ```

可选——安装 Agent Skill，让 AI 自动掌握体检、建对话、排错与派单流程：

```bash
npx skills add xsj0413abc-ui/gemini-task-bridge
```

或在 Claude Code 插件市场安装：

```text
/plugin marketplace add xsj0413abc-ui/gemini-task-bridge
/plugin install gemini-task-bridge@gemini-task-bridge
```

### 5. 新开会话，跑通第一单

在你的 AI 宿主里**新开一个会话**，直接让它新建 Gemini 对话并派发任务，例如：

> 「用 gemini-task-bridge 新建一个叫 `Code Reviewer` 的 Gemini Task 对话，请它审阅 `/abs/path/to/spec.md`。」

AI 会自动调用 `gemini_pair_init`（由后台服务代建，宿主无需辅助功能权限），接着执行 `gemini_send` → `gemini_wait` → `gemini_read`。

---

## 核心特性（v2.2.0）

- **AI 自主新建 Gemini 对话（后台控制面）**：
  - `gemini_pair_init`、`gemini_list_chats`（以及 CLI 的 `pair init` / `pair gemini-chats`）通过原子控制队列交给后台 LaunchAgent 执行，AI 宿主进程永远不需要辅助功能权限。
  - 每个 `--scope` 上限 **10 个启用配对**，支持幂等键防重与崩溃中断恢复。
- **报错说人话 + 分阶段 `doctor` 体检**：
  - 所有 CLI 和 MCP 报错都自带大白话 `message` 与下一步操作 `fix`（支持中英文切换，优先读 `GEMINI_TASK_BRIDGE_LANG=zh|en`，其次看 `LANG`/`LC_ALL`，默认英文）。
  - `gemini-task-bridge doctor` 以后台健康状态为准，按阶段输出 `✅/❌` 与下一步指令。
- **多宿主隔离的零依赖 Stdio MCP Server（`gemini-task-bridge mcp`）**：
  - 纯 Python 3.9+ 标准库实现 JSON-RPC 2.0，兼容 MCP `2025-06-18`、`2025-03-26`（支持 batch）与 `2024-11-05`。
  - 通过 `--scope <name>` 隔离不同宿主的配对与任务；工具参数同时兼容蛇形与驼峰（`pair_id`/`pairId`、`task_id`/`taskId`、`idempotency_key`/`idempotencyKey`）；默认 `--adapter-filter local`，不碰线上 T3 配对。
- **崩溃安全队列与零焦点抢占**：
  - 三段式隐形暂存 + `atomic_rename_excl`（`st_nlink == 1`）、Pre-AX `dispatching` 过渡态与 Step C 锁内复验，通过 `AXValue`/`AXPress` 静默投递，不占剪贴板也不抢前台焦点。
- **中英文双语 `needs_attention` 检测与 TCC 防呆**：
  - 同时识别英文 `Needs attention` 与中文 `需要注意` 徽标及确认卡片，暂停等待人工确认而不越权自动点击。
  - 绑定单硬链接 `Python.app` 防止 macOS `tccd` 误归因到 `/usr/bin/git`；安装服务时自动拦截指向同一数据目录的冲突 LaunchAgent。

---

## T3 Code 接入（`adapter=t3`）

T3 Code 用户可继续使用完整的 1 对 1 双向线程调度：Gemini 回复通过官方 `t3 thread send` 自动送回目标线程，保留平铺 `sent/<taskId>.json` + `sent/<taskId>.receipt.json` 归档，并支持 `mode t3-all` 一键回切原生 T3。（`t3` CLI 仅在使用 `adapter=t3` 配对时才需要。）

```bash
# 新建 T3 Code <-> Gemini Task 双向配对
gemini-task-bridge pair init \
  --id "t3_reviewer" \
  --adapter t3 \
  --t3-thread "<目标T3线程ID>" \
  --reply-to "<接收回报的T3线程ID>" \
  --gemini-title "T3 Code Reviewer" \
  --workspace "/abs/path/to/project"

# 派发任务（完成后自动回传到 --reply-to）
gemini-task-bridge send --pair t3_reviewer --message "请审阅 /abs/path/to/spec.md"

# 紧急回切 / 单配对切路由
gemini-task-bridge mode t3-all               # 所有 t3 配对切回原生 T3，local 配对安全暂停
gemini-task-bridge mode hybrid               # 恢复按配对路由
gemini-task-bridge pair route t3_reviewer t3 # 单独切换某个 t3 配对
```

---

## 进阶：最小权限隔离（`--allow-pairs`）

默认情况下，`gemini-task-bridge mcp --scope <宿主名>` 允许该宿主访问自己创建的配对（`ownerScope == scope`）。如果需要把某个项目 Agent 锁死在指定的预建配对上，或让某个作用域额外共享一个配对，可配合 `--allow-pairs`：

```bash
# 不设 --scope，把项目 Agent 严格限制在单个预建 local 配对上
gemini-task-bridge mcp --adapter-filter local --allow-pairs reviewer

# 或在 --scope 基础上额外开放一个共享配对
gemini-task-bridge mcp --scope claude-code --allow-pairs shared_reviewer
```

`--allow-pairs` 为空、含空白段或格式非法时启动即 fail-closed 报错（退出码 `2`）。完整宿主机 TOML/JSON 模板与工具参数见 [mcp-and-multi-agent.md](skills/gemini-task-bridge/references/mcp-and-multi-agent.md)。

---

## 从 `t3-gemini-bridge` 升级

v2.2.0 将项目从 `t3-gemini-bridge` 更名为 `gemini-task-bridge`，**老用户无需搬迁数据，平滑兼容**：

- **命令名**：安装 `~/.local/bin/gemini-task-bridge` 的同时保留静默兼容入口 `t3-gemini-bridge`（不向 stdout 输出任何内容，确保 MCP stdio 安全）。
- **数据目录**：若 `~/.local/share/t3-gemini-bridge` 已存在且新目录不存在，会自动继续沿用旧目录，不自动搬迁数据。
- **环境变量与 LaunchAgent**：所有 `GEMINI_TASK_BRIDGE_*` 变量未设时自动回读对应的 `T3_GEMINI_BRIDGE_*`；指向同一目录的已有 LaunchAgent Label 会被自动复用，遇到冲突 Label 时会以 `conflicting_launchagent_for_root` 拦截并给出处理命令。

完整改名兼容表见 [cli-reference.md](skills/gemini-task-bridge/references/cli-reference.md)，旧版 `v2.0` 生产受控切换步骤见 [macos-permissions-and-troubleshooting.md](skills/gemini-task-bridge/references/macos-permissions-and-troubleshooting.md)。

---

## 架构概览

```text
Claude Code / Codex / Antigravity / T3 Code
  │
  ├──(MCP stdio / CLI: pair init, list_chats)──▶ control/inbox/*.json ─────────┐
  └──(MCP stdio / CLI: send, task wait|read)───▶ inbox/*.task.json ────────────┤
                                                                               ▼
`sent/payloads/` (local) /                                    LaunchAgent 守护进程 (Python.app)
`t3 thread send` (t3)     ◀── outbox/<taskId>.json ◀───────── AXValue + AXPress (静默投递)
                              (或 AX 正文抓取)                Gemini.app (Task 任务模式)
```

---

## 仓库目录

```text
gemini-task-bridge/
├── .claude-plugin/                         # Claude Code 插件与市场清单
├── .github/workflows/ci.yml                # Python 3.12 与纯标准库 Python 3.9 CI
├── skills/gemini-task-bridge/
│   ├── SKILL.md                            # Agent Skill 操作手册
│   ├── agents/openai.yaml                  # UI 元数据与触发策略
│   ├── references/                         # CLI、MCP、macOS 权限排错与安全架构文档
│   └── scripts/
│       ├── gemini_task_bridge.py           # 桥接 CLI、适配器、队列与 LaunchAgent 管理
│       ├── control_plane.py                # 后台控制队列、配额与中断恢复
│       ├── error_catalog.py                # 中英文错误说明与修复建议
│       ├── bridge_ax.py                    # macOS Accessibility 与 DiagnosticLogWatcher 驱动
│       ├── mcp_server.py                   # 零依赖标准库 stdio MCP Server
│       └── install.sh                      # 原子无损本地安装脚本
└── tests/                                  # 离线单元与 stdio MCP 回归测试
```

---

## 运行单元测试

```bash
python3 -B -m unittest discover -s tests -v
```

---

## 安全与隐私设计

- 运行时目录统一 `0o700`，状态与控制文件统一 `0o600`（`umask 0o077`）。
- 所有文件读取拒绝软链接（`O_NOFOLLOW` + `no_symlink()`），校验 `st_nlink == 1` 与 `st_uid == os.getuid()`，严格限制体积与行数。
- 控制请求在后台全量重新校验；建对话中途崩溃绝不盲目重复操作 `Gemini.app` UI。
- Outbox 回执按阶段门与接单冻结快照（`pairId`、`pairUid`、`adapter`、`replyThreadId`）交叉核验。

---

## 开源协议

[MIT](LICENSE) © 2026 xsj0413abc-ui
