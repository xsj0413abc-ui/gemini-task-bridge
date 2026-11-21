# t3-gemini-bridge

[English](README.md) | **简体中文**

零第三方依赖的 **T3 Code ⇄ Google Gemini 桌面端（Task 模式）1 对 1 全自动双向桥接工具与 Agent 技能**（macOS）。

---

## 为什么做这个项目

- **T3 Code** 擅长多线程 Agent 编排（Claude Code、Codex、Antigravity 等），而 **Google Gemini 桌面端（`Gemini.app`）Task 模式**具备独立配额与桌面原生任务能力。
- 但两边之间缺少官方的自动对接通道——以往需要在两个窗口之间手动复制任务单、喊「开工」、再把结果拷回 T3。
- `t3-gemini-bridge` 基于 macOS 原生辅助功能（`AXUIElement`）与官方 `t3` CLI 打通全自动双向流转：**零 `pip` 第三方依赖**、**不占剪贴板也不抢前台焦点**，且**随时一条命令瞬时切回全 T3 对话**。

---

## 核心特性

- **全自动往返（免手动喊「开工」）**：
  - **去程**：`t3-gemini-bridge send` 将任务原子写入 `inbox/`；后台 LaunchAgent 守护进程静默切换到绑定的 Gemini 对话，通过 `AXValue` 注入并用 `AXPress` 提交，不占用系统剪贴板，不抢占当前窗口焦点。
  - **回程**：支持 Gemini 写入结构化 `outbox/<taskId>.json`（主通道）以及防抖后原生 AX 正文提取（兜底通道），经官方 `t3 thread send` 自动送回目标 T3 线程。
- **严格 1 对 1 隔离与 4 层防串台保护**：
  - `pairs.json` 强制校验任意两个启用中的配对不得复用同一 `t3ThreadId` 或 `geminiTitle`。
  - 每次注入/抓取前强制核验 `AXWindow.AXTitle == geminiTitle`，自动避让忙碌态与人工确认卡片（`in_progress` / `needs_attention` / 停止按钮），单配对 FIFO 串行、跨配对并发。
- **分段哨兵防提示词回显（Prompt-Echo Immunity）**：
  - 使用 `[[T3_BRIDGE_START:<taskId>]]` 与拆分式完成标记指令包裹派单，避免 Gemini 界面回显提问时误触发完成，并在多轮对话或折叠气泡中精准切片当轮正文。
- **随时一键切回全部用 T3（`mode t3-all`）**：
  - 执行 `t3-gemini-bridge mode t3-all` 后，所有 `send` 自动直投 `pair.t3ThreadId`，完全不碰 Gemini；也可通过 `pair route <pairId> t3` 单独切换某个岗位。
- **macOS TCC `Python.app` 权限防抖加固**：
  - 规避 macOS `/usr/bin/python3` 与 `/usr/bin/git` 等 78 个工具 shim 共用同一 inode 导致的 `tccd` 误归因掉权问题，LaunchAgent 自动绑定独立单硬链接 `Python.app` 主二进制。
- **中英文 Gemini 客户端双语兼容**：
  - 完整适配简体中文（`zh_CN`）与英文（`en`）的 `Gemini.app` 边栏状态徽标、输入框、文件夹芯片与确认卡片。

---

## 架构概览

```text
T3 Code 对话线程 ──(send)──▶ inbox/*.task.json ──▶ LaunchAgent 守护进程 (Python 3.9+ 标准库)
      ▲                                                  │
      │                                       AXValue + AXPress 静默注入
      │                                                  ▼
`t3 thread send` ◀── outbox/<taskId>.json ◀──── Gemini.app (Task 任务模式)
                     (或 AX 回复自动抓取)
```

- **不窃取内部凭据、不写私有数据库**：不读取/复用 T3 内部 Provider Token，不直接写 `statev2.sqlite` 或 Gemini 的 SwiftData 数据库。
- **Fail-Closed 幂等投递**：调用 `t3` 前先原子切入 `sending/`；若超时或中断则转为 `unknown_timeout` 并移入 `failed/`，绝不盲目重试导致重复执行；重复回执自动归档 `dedup/`。

---

## 安装指南

### 1. 安装 Agent Skill

使用 Vercel `skills` CLI（兼容 T3 Code、Claude Code、Codex、Antigravity、Cursor 等）：

```bash
npx skills add xsj0413abc-ui/t3-gemini-bridge
```

或在 Claude Code 中以插件形式安装：

```text
/plugin marketplace add xsj0413abc-ui/t3-gemini-bridge
/plugin install t3-gemini-bridge@t3-gemini-bridge
```

### 2. 安装本机 macOS 桥接 CLI 与后台守护服务

前置依赖：**macOS**、**Python 3.9+**、**T3 Code**（`~/.t3/bin/t3` 或 `PATH` 中的 `t3` CLI）、**Google Gemini 桌面端**（`/Applications/Gemini.app`）。

```bash
git clone https://github.com/xsj0413abc-ui/t3-gemini-bridge.git
cd t3-gemini-bridge
bash skills/t3-gemini-bridge/scripts/install.sh
```

安装脚本会打印解析出的单硬链接 `TCC Python` 路径（一个 `Python.app` 可执行文件）：

1. 打开 **系统设置 → 隐私与安全性 → 辅助功能**，添加并勾选该 `Python.app`。
2. 若绑定的工作目录位于 `~/Desktop`、`~/Documents` 或 `~/Downloads`，请在 **完全磁盘访问权限** 中一并勾选该 `Python.app`。
3. 安装并启动 LaunchAgent，运行体检：

```bash
t3-gemini-bridge service install
t3-gemini-bridge doctor --json
```

确认以下 4 项均为绿色通过：
- `launchAgent.state == "running"`
- `daemonHealth.axTrusted == true`
- `daemonHealth.screenLocked == false`
- `daemonHealth.geminiRunning == true`

---

## 快速上手

### 建立 T3 与 Gemini Task 对话的 1 对 1 绑定

**方式 A：在 Gemini 新建专属「任务」对话并自动挂载工作区（推荐）：**

> 提示：目标工作目录需先在 `Gemini.app` 的「文件夹」选择器中打开/收藏过一次，才能被自动选中。

```bash
t3-gemini-bridge pair init \
  --id "reviewer" \
  --label "Code Reviewer" \
  --t3-thread "<目标T3线程ID>" \
  --reply-to "<接收回报的T3线程ID>" \
  --gemini-title "Code Reviewer" \
  --workspace "/abs/path/to/project"
```

**方式 B：绑定一个 Gemini 桌面端中已有的对话：**

```bash
t3-gemini-bridge pair gemini-chats --json
t3-gemini-bridge pair add \
  --id "reviewer" \
  --t3-thread "<目标T3线程ID>" \
  --reply-to "<接收回报的T3线程ID>" \
  --gemini-title "<Gemini边栏中的精确标题>"
```

### 自动派单与接收自动回报

```bash
# 异步派单（立即返回 taskId；Gemini 完成后自动回传到 --reply-to 线程）
t3-gemini-bridge send --pair reviewer --message "请审阅 /abs/path/to/spec.md 并列出阻塞项。"

# 从文件或标准输入读取长任务单
t3-gemini-bridge send --pair reviewer --file /abs/path/to/task.md

# 同步等待本单往返完成并输出 JSON 回执
t3-gemini-bridge send --pair reviewer --wait --timeout 300 --json --message "..."
```

### 随时一键切回全部 T3 / 单对切换

```bash
# 全局一键切回全部用 T3 原生对话（零 Gemini 触碰）
t3-gemini-bridge mode t3-all

# 恢复混合路由
t3-gemini-bridge mode hybrid

# 仅切换单个配对
t3-gemini-bridge pair route reviewer t3
t3-gemini-bridge pair route reviewer gemini
```

---

## 仓库目录

```text
t3-gemini-bridge/
├── .claude-plugin/                  # Claude Code 插件与市场清单
├── .github/workflows/ci.yml         # CI 单元测试、Skill 规范与脱敏校验
├── skills/t3-gemini-bridge/
│   ├── SKILL.md                     # Agent Skill 主指令（渐进式披露）
│   ├── agents/openai.yaml           # UI 元数据与触发策略
│   ├── references/                  # CLI 手册、TCC 权限排查与架构安全文档
│   └── scripts/
│       ├── bridge_ax.py             # macOS Accessibility 与 DiagnosticLogWatcher 驱动
│       ├── t3_gemini_bridge.py      # 桥接 CLI、队列状态机与 LaunchAgent 管理
│       └── install.sh               # 无损本地安装脚本
└── tests/
    └── test_bridge.py               # 26 项离线单元测试（含 Mock AX 与模拟 t3 CLI）
```

---

## 运行单元测试

```bash
python3 -B -m unittest discover -s tests -v
```

---

## 安全与隐私设计

- 运行时目录统一为 `0o700`，状态与账本文件为 `0o600`（`umask 0o077`）。
- 文件读取全链路拒绝软链接（`O_NOFOLLOW` + 父路径 `no_symlink()` 检查），限制体积与硬链接数，并校验读取前后文件未篡改。
- 回报接收线程 `replyThreadId` 受严格白名单限制。
- 仓库代码与提交历史中不包含任何个人路径、密钥或账号标识。

---

## 开源协议

[MIT](LICENSE) © 2026 xsj0413abc-ui
