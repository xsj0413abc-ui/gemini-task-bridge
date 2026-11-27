# t3-gemini-bridge

**验证状态：** 离线桥接与 stdio MCP 回归测试覆盖 Python 3.9 和 3.12；Codex、Claude Code、Antigravity 的真实宿主端到端验收（Stage B）仍待完成。配置示例是接入说明，不代表已完成对应宿主实测。详见[验收清单](skills/t3-gemini-bridge/references/mcp-and-multi-agent.md#3-two-stage-acceptance-checklist)。

[English](README.md) | **简体中文**

零第三方依赖的 **Google Gemini 桌面端（`Gemini.app` Task 模式）1 对 1 全自动桥接工具、任务 CLI 与 stdio MCP Server**（macOS），支持 **T3 Code**、**Claude Code**、**Codex** 与 **Antigravity**。

---

## 为什么做这个项目

- **T3 Code**、**Claude Code**、**Codex**、**Antigravity** 擅长本地写码、测试与 Agent 编排，而 **Google Gemini 桌面端（Task 模式）**具备桌面原生任务能力、本地文件夹上下文与独立配额。
- 以往缺少自动对接通道，需要手工切换窗口复制粘贴、喊「开工」、再把结果搬回去。
- `t3-gemini-bridge` 通过 macOS 原生辅助功能（`AXUIElement`）、原子化文件队列、零依赖 stdio MCP Server 与可选的官方 `t3 thread send` 打通全自动流转：**零 `pip` 第三方依赖**、**不占剪贴板也不抢前台焦点**。

---

## 核心特性

- **多客户端通用（`adapter=local` + `adapter=t3`）**：
  - **Claude Code / Codex / Antigravity（`local`）**：无需安装 `t3` CLI。通过作用域隔离的 stdio MCP（`t3-gemini-bridge mcp`）或 CLI（`send` / `task status|wait|read`）调度 Gemini，正文经 SHA-256 校验归档至 `sent/payloads/`。
  - **T3 Code（`t3`）**：完整保留 1 对 1 双向线程调度、平铺 `sent/<taskId>.json` + `sent/<taskId>.receipt.json` 兼容产物，以及 `mode t3-all` 一键切回原生 T3。
- **零依赖 Stdio MCP Server（`t3-gemini-bridge mcp`）**：
  - 纯 Python 3.9+ 标准库实现 JSON-RPC 2.0，兼容 MCP `2025-06-18`、`2025-03-26`（支持 batch）与 `2024-11-05`。
  - 默认 `--adapter-filter local`，配合 `--allow-pairs` fail-closed 白名单，严格隔离项目会话与线上 T3 生产配对。
  - 默认非阻塞 `gemini_send` 立即返回持久 `taskId`，配合分片 `gemini_wait`、三路并发通道（`ping`/取消帧永不阻塞）与单请求取消。
- **单链接原子发布与 Pre-AX `dispatching` 保护**：
  - 采用三段式隐形暂存 + `atomic_rename_excl`（macOS `renamex_np` / Linux `renameat2`）保证 `st_nlink == 1`；在调用 `AXPress` 前落盘 `dispatching` 过渡态并在重新锁定 `.gemini_ui.lock` 核对标题后再提交，杜绝误发与重发。
- **统一 `needs_attention` 检测（中英文双语）**：
  - 同时监控侧栏徽标（`需要注意` / `Needs attention`）与窗口内确认卡片，发现即暂停而不越权自动点击确认；手动确认后自动续跑。
- **macOS TCC `Python.app` 权限加固**：
  - 自动绑定单硬链接 `Python.app` 主二进制，避免 macOS `tccd` 因 `/usr/bin/python3` shim 误归因到 `/usr/bin/git` 导致掉权。

---

## 架构概览

```text
T3 Code / Claude Code / Codex / Antigravity
  │
  ├──(CLI: send / task)──▶ 三段式暂存 + atomic_rename_excl ──▶ inbox/*.task.json
  └──(MCP: stdio)──────┘                                               │
                                                                       ▼
`t3 thread send` (t3) /                              LaunchAgent 守护进程 (Python 3.9+ stdlib)
`sent/payloads/` (local) ◀── outbox/<taskId>.json ◀─ AXValue + AXPress (静默，Step C 锁内复验)
                             (或 AX 正文抓取)         Gemini.app (Task 任务模式)
```

---

## 安装指南

### 1. 安装 Agent Skill

```bash
npx skills add xsj0413abc-ui/t3-gemini-bridge
```

或在 Claude Code 中以插件形式安装：

```text
/plugin marketplace add xsj0413abc-ui/t3-gemini-bridge
/plugin install t3-gemini-bridge@t3-gemini-bridge
```

### 2. 安装本机 macOS 桥接 CLI、MCP Server 与守护服务

前置依赖：**macOS**、**Python 3.9+**、**Google Gemini 桌面端**（`/Applications/Gemini.app`）。（`t3` CLI 仅在使用 `adapter=t3` 配对时才需要。）

```bash
git clone https://github.com/xsj0413abc-ui/t3-gemini-bridge.git
cd t3-gemini-bridge
bash skills/t3-gemini-bridge/scripts/install.sh
```

安装脚本会打印解析出的单硬链接 `TCC Python` 路径（`Python.app`）：

1. 在 **系统设置 → 隐私与安全性 → 辅助功能** 中启用该 `Python.app`。
2. 若绑定的工作目录位于 `~/Desktop`、`~/Documents` 或 `~/Downloads`，在 **完全磁盘访问权限** 中一并启用该 `Python.app`。
3. 安装并体检 LaunchAgent：

```bash
t3-gemini-bridge service install
t3-gemini-bridge doctor --json
```

---

## 快速上手

### 方式一：Codex / Claude Code / Antigravity（`adapter=local` + MCP 或 CLI）

```bash
# 1. 创建专属 Gemini Task 对话（无需 T3 线程）
t3-gemini-bridge pair init \
  --id "reviewer" \
  --adapter local \
  --label "Code Reviewer" \
  --gemini-title "Code Reviewer" \
  --workspace "/abs/path/to/project"

# 2. 在 Codex 或 Claude Code 中注册受限作用域的 MCP Server
codex mcp add gemini-bridge -- "$HOME/.local/bin/t3-gemini-bridge" mcp --adapter-filter local --allow-pairs reviewer
# 或：
claude mcp add --transport stdio gemini-bridge -- "$HOME/.local/bin/t3-gemini-bridge" mcp --adapter-filter local --allow-pairs reviewer

# 3. 或通过 CLI 生命周期命令调用
t3-gemini-bridge send --pair reviewer --message "请审阅 /abs/path/to/spec.md"
t3-gemini-bridge task wait <taskId> --return-on-attention --json
t3-gemini-bridge task read <taskId> --json
```

更多宿主机原生 TOML / JSON 配置模板见 [skills/t3-gemini-bridge/references/mcp-and-multi-agent.md](skills/t3-gemini-bridge/references/mcp-and-multi-agent.md)。

### 方式二：T3 Code 1 对 1 双向配对（`adapter=t3`）

```bash
t3-gemini-bridge pair init \
  --id "t3_reviewer" \
  --t3-thread "<目标T3线程ID>" \
  --reply-to "<接收回报的T3线程ID>" \
  --gemini-title "T3 Code Reviewer" \
  --workspace "/abs/path/to/project"

t3-gemini-bridge send --pair t3_reviewer --message "请审阅 /abs/path/to/spec.md"
t3-gemini-bridge mode t3-all   # 一键切回全部使用原生 T3 对话
```

---

## 仓库目录

```text
t3-gemini-bridge/
├── .claude-plugin/                  # Claude Code 插件与市场清单
├── .github/workflows/ci.yml         # Python 3.12 与纯标准库 Python 3.9 CI
├── skills/t3-gemini-bridge/
│   ├── SKILL.md                     # Agent Skill 主指令
│   ├── agents/openai.yaml           # UI 元数据与触发策略
│   ├── references/                  # CLI、MCP/多客户端、TCC 权限与切换安全文档
│   └── scripts/
│       ├── bridge_ax.py             # macOS Accessibility 与 DiagnosticLogWatcher 驱动
│       ├── mcp_server.py            # 零外部依赖标准库 stdio MCP Server
│       ├── t3_gemini_bridge.py      # 桥接 CLI、适配器、队列与 LaunchAgent 管理
│       └── install.sh               # 原子无损本地安装脚本
└── tests/
    ├── test_bridge.py               # 离线桥接、切换、崩溃切点与 ACL 单元测试
    └── test_mcp.py                  # Stdio MCP 协议与并发单元测试
```

---

## 运行单元测试

```bash
python3 -B -m unittest discover -s tests -v
```

---

## 安全与隐私设计

- 运行时目录统一 `0o700`，状态与账本文件 `0o600`（`umask 0o077`）。
- 文件读取拒绝软链接（`O_NOFOLLOW` + `no_symlink()`），严格限制体积与硬链接数（`st_nlink == 1`）。
- Outbox 回执按阶段门与接单冻结快照（`pairId`、`pairUid`、`adapter`、`replyThreadId`）交叉核验。
- MCP 默认隔离为 `local` 范围，支持 fail-closed `--allow-pairs`，且不泄露全局统计或他处侧栏对话标题。

---

## 开源协议

[MIT](LICENSE) © 2026 xsj0413abc-ui
