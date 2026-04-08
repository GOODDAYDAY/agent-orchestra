[English](README.md) | [简体中文](README.zh-CN.md)

---

# Agent Orchestra

> 一个本地 TUI，通过**编排器**（Orchestrator）—— 一个 LLM 驱动的自主调度者 —— 指挥一队 Claude Code CLI agent 完成软件交付工作流。编排器坐在 5 个角色化下属（PM、技术总监、开发、测试、用户）中间，通过 tmux 向它们派发任务；并借助 `/req-*` 技能目录驱动完整的 `分析 → 设计 → 编码 → 安全 → 清理 → 复审 → 验证 → 归档` 流水线。

---

## 目录

- [Agent Orchestra 是什么](#agent-orchestra-是什么)
- [为什么存在](#为什么存在)
- [架构总览](#架构总览)
- [依赖要求](#依赖要求)
- [安装](#安装)
- [快速开始](#快速开始)
- [交互模型](#交互模型)
- [Orchestrator 协议](#orchestrator-协议)
- [内置工作流](#内置工作流)
- [技能目录](#技能目录)
- [键盘快捷键](#键盘快捷键)
- [配置](#配置)
- [数据目录结构](#数据目录结构)
- [开发](#开发)
- [项目历史（REQ 驱动）](#项目历史req-驱动)
- [已知限制](#已知限制)

---

## Agent Orchestra 是什么

Agent Orchestra 是一个基于 **Textual** 的本地 TUI，跑在 `tmux` 和 **Claude Code CLI** 之上。它允许你创建一个 *group*（组） —— 每个 agent 是一个真实的 Claude CLI 进程，运行在自己的 tmux pane 中 —— 并让它们按照工作流剧本协作完成一个软件任务。**工作不是由 Python 脚本化的**：它由一个 LLM **Orchestrator** agent 在运行时动态派发。这个编排者读取最新的工作结果，决定接下来该谁做、做什么（包括是否需要调用某个 `/req-*` 技能）。

具体来说，当你用 `standard` 工作流启动一个 group 时，你会得到 **6 个** 并发运行的 Claude CLI 进程：

```
┌─ tmux session: agent-mgmt-<group-id> ─────────────────────────────────┐
│                                                                       │
│  ╔═════════════╗  ╔═════════════╗  ╔═════════════╗  ╔═════════════╗   │
│  ║ Product     ║  ║ Tech        ║  ║ Developer   ║  ║ Tester      ║   │
│  ║ Manager     ║  ║ Director    ║  ║             ║  ║             ║   │
│  ║ (claude CLI)║  ║ (claude CLI)║  ║ (claude CLI)║  ║ (claude CLI)║   │
│  ╚══════▲══════╝  ╚══════▲══════╝  ╚══════▲══════╝  ╚══════▲══════╝   │
│         │ send-keys      │                │                │          │
│         │ capture-pane   │                │                │          │
│         │                │                │                │          │
│  ╔══════▼══════╗  ┌──────┴────────────────┴────────────────┘          │
│  ║    User     ║  │                                                    │
│  ║ (claude CLI)║  │                                                    │
│  ╚══════▲══════╝  │                                                    │
│         │         │                                                    │
│         └─────────┤                                                    │
│                   │                                                    │
│          ╔════════▼═════════╗                                          │
│          ║   Orchestrator   ║  ◀── 第 6 个 Claude CLI 进程。它的 system │
│          ║   (claude CLI)   ║      prompt 携带工作流、下属名单、以及    │
│          ║                  ║      /req-* 技能目录。动态决定下一步该    │
│          ║ 产出 <<DISPATCH  ║      dispatch 给谁、什么时候跳过步骤、    │
│          ║ role="dev"       ║      什么时候让 tester 循环到 dev、以及   │
│          ║  text="...">>    ║      什么时候判定工作流完成。            │
│          ║                  ║                                           │
│          ╚══════════════════╝                                          │
└───────────────────────────────────────────────────────────────────────┘
```

在 tmux 之上是一个 **Textual TUI**：

- 每个 pane 用只读预览（read-only preview）实时显示内容，支持 ANSI 颜色渲染和滚动锁
- 可以点击任意 pane 上的 `Enter` 跳进去进行完全原生的终端交互（或在预览聚焦时按 Enter）
- 有一个专门的**输入框**（input box），可实时转发每一次按键 —— 字母、数字、标点、方向键、Tab、Ctrl+ 组合键 —— 直接送到被聚焦的 agent
- 有一个 8 键**快捷按键盘**（`Continue` `Y` `N` `Esc` `^C` `↑` `↓` `^D`）用于一键回应 Claude CLI 的交互提示
- 管理行（`Pause / Resume / Edit / Restart / Delete`）默认折叠在 `⋯` 按钮后面以节省竖向空间
- 底部日志面板显示工作流生命周期事件（dispatch、worker result、stall、complete、abort）

---

## 为什么存在

同时运行多个 Claude Code CLI 实例很有用，但操作上很痛苦：你得手动在它们之间搬运上下文、在不同终端之间复制粘贴结果、记住谁该做下一步、管理残留的 session。现有的多 agent 框架能做到这些，但通常需要你把整个代码库重新接到它们的运行时上。

Agent Orchestra 的赌注和其他方案不一样：

1. **保持 agent 不变。** 每个 worker 都是一个普通的 `claude --dangerously-skip-permissions` 进程。你对 Claude Code CLI 已有的所有认知（slash command、MCP 工具、技能系统、用 tmux attach 调试）都继续有效。
2. **让 LLM 编排者决定顺序。** 不是在 Python 里硬编码一个状态机，而是让编排者本身是一个 Claude CLI —— 带一份精心设计的 system prompt。它读取每个 worker 的输出，选择下一个动作，自己做错误恢复。
3. **用 tmux 做传输层。** 没有 MCP server、没有自定义 RPC、没有供应链风险。`send-keys` 写、`capture-pane` 读。出问题时你可以 `tmux attach -t agent-mgmt-<group-id>` 直接看到每个 agent 看到的东西。
4. **交互必须原生。** TUI 从不假装自己是一个它做不到的终端。真实时输入用纯按键转发的输入框；需要 Tab 补全 / 粘贴 / 菜单 / ANSI 光标应用时，按 Enter `tmux switch-client` 进入真实的 worker pane。

---

## 架构总览

```
                              ┌─────────────────────┐
                              │  Textual TUI (app)  │
                              │                     │
                              │  - AgentPane × N    │
                              │  - GroupPanel       │
                              │  - EventLog         │
                              │  - 模态对话框        │
                              └──────────┬──────────┘
                                         │ 用户操作
                                         ▼
                              ┌─────────────────────┐
                              │   Supervisor        │
                              │                     │
                              │  - start/stop_group │
                              │    (asyncio.gather  │
                              │     并发化)          │
                              │  - dispatch_loop    │
                              │    (orchestrator    │
                              │     pane 轮询)       │
                              │  - force_advance /  │
                              │    abort_workflow   │
                              └──────────┬──────────┘
                                         │
                   ┌─────────────────────┼─────────────────────┐
                   │                     │                     │
                   ▼                     ▼                     ▼
        ┌──────────────────┐  ┌───────────────────┐  ┌───────────────────┐
        │ SessionManager   │  │ orchestrator.py   │  │ workflows.py      │
        │                  │  │                   │  │                   │
        │ - tmux 辅助函数   │  │ 纯函数:           │  │ 剧本数据:         │
        │ - send_keys      │  │ - parse_latest_   │  │ - STANDARD        │
        │ - send_raw_keys  │  │     dispatch      │  │ - PROTOTYPE       │
        │ - capture_pane_  │  │ - detect_         │  │ - RESEARCH        │
        │     full (ansi)  │  │     completion    │  │                   │
        │ - start_agent_   │  │ - is_workflow_    │  │ 技能目录:         │
        │     session      │  │     complete /    │  │ - AVAILABLE_      │
        │ - _render_       │  │     abort         │  │     SKILLS        │
        │     orchestrator │  │ - validate_       │  │                   │
        │     _prompt      │  │     dispatch_text │  │ 角色能力:         │
        │ - _wait_for_     │  │                   │  │ - ROLE_           │
        │     pane_ready   │  │                   │  │     CAPABILITIES  │
        │     (F-04 轮询)  │  │                   │  │                   │
        └──────────┬───────┘  └───────────────────┘  │ 渲染函数:         │
                   │                                  │ - render_for_     │
                   │ tmux 子进程                       │     orchestrator  │
                   ▼                                  │ - render_roster   │
        ┌──────────────────┐                          │ - render_skill_   │
        │ tmux panes       │                          │     catalogue    │
        │                  │                          └───────────────────┘
        │ 每组 6 个         │
        │ Claude CLI       │
        └──────────────────┘
                   │
                   ▼
        ┌──────────────────┐
        │ SQLite           │
        │ .agent_management│
        │   /state.db      │
        │                  │
        │ - groups         │
        │ - agents         │
        │ - sessions       │
        │ - role_templates │
        │ - meta           │
        └──────────────────┘
```

**关键要点：**

- **没有 MCP server，没有事件总线。** 编排者与 worker 之间完全通过 `tmux send-keys` 和 `tmux capture-pane` 通信。早期版本曾用一个 MCP pub/sub 系统；REQ-012 v2 在确认"LLM 没有收件箱"使事件总线模型从根上不可靠之后彻底删除了它。
- **Worker 完全不知道编排者的存在。** 它们的 system prompt 中从不提 "Orchestrator"、"dispatch" 或 "[WORKER_RESULT]"。它们只知道："收到一个任务，做它，在最后一行输出 `<<TASK_DONE>>`，停下"。worker → 编排者的回调路径在 Python 代码层由 `Supervisor.dispatch_loop` 强制执行，不依赖任何 prompt 配合。
- **工作流是剧本，不是状态机。** 三个内置工作流（`standard`、`prototype`、`research`）被渲染到编排者的 system prompt 里作为*典型流程*。编排者被明确告知："这是建议，不是硬性要求，根据每次的情况自主编排"。
- **破坏性 schema 变更不提供迁移。** 启动时如果 `meta.schema_version` 与当前 `SCHEMA_VERSION` 不匹配，app 弹出重置模态框并提供"擦掉 `.agent_management/`"的选项。没有迁移脚本 —— 这是个本地单用户工具。

---

## 依赖要求

| 依赖 | 版本 | 说明 |
|:---|:---|:---|
| Python | 3.13+ | 默认通过 `uv` 使用 |
| [uv](https://docs.astral.sh/uv/) | 最新版 | 最快的安装和运行方式 |
| tmux | 3.0+ | 平台依赖 `switch-client` 和现代 send-keys 语义 |
| [Claude Code CLI](https://docs.claude.com/claude-code) | 最新版 | `claude --dangerously-skip-permissions` 必须可用 |
| 操作系统 | macOS / Linux / Windows | Windows 通过 Git Bash/MSYS 使用 tmux；在 Windows 11 + `plink` tmux 转发上测试过 |

Python 运行时只依赖两个外部包：`textual` 和 `aiosqlite`。其他一切（ANSI 渲染、sqlite、asyncio、subprocess）都是标准库或 Textual 间接引入的。

---

## 安装

```bash
# 克隆仓库（submodule 会拉取 .claude/skills 工具包）
git clone --recurse-submodules git@github.com:GOODDAYDAY/agent-orchestra.git
cd agent-orchestra

# 同步依赖到本地 .venv
uv sync
```

验证安装：

```bash
uv run python -m agent_management --show-config
```

这会打印解析后的路径、Claude CLI 命令、以及所有调优常量，但不启动 TUI。如果正常完成，你就可以开始使用了。

---

## 快速开始

### 启动 TUI

**macOS / Linux：**
```bash
bash scripts/start.sh
```

**Windows（Git Bash）：**
```bash
scripts/start.bat
```

启动脚本会设置 `AGENT_MGMT_DATA_DIR=<project>/.agent_management/` 让所有运行时状态放在项目目录（而不是 `~/`），并在启动前执行 `uv sync`。

### 创建一个 group

1. 按 `g`（或点 `+ Group`）打开"新建 Group"对话框
2. 输入 group 名（例如 `sprint-01`）
3. 输入工作目录（支持路径自动补全，Tab 接受建议）
4. 选一个工作流：**Standard**、**Prototype**、或 **Research**（见 [内置工作流](#内置工作流)）
5. 点 **Create**

平台会自动创建 **6 个** agent：Orchestrator + PM + 技术总监 + Developer + Tester + User，它们共享同一个工作目录。在 group 面板里可以看到这些 agent。

### 启动 group

点 **▶ Start**。平台会：

1. 创建名为 `agent-mgmt-<group-id>` 的 tmux session
2. **并行**启动 5 个 worker agent（并发的 `tmux new-window` + Claude CLI 启动 + readiness 轮询）
3. 等待所有 5 个 worker 达到 `active` 状态
4. 用实时的 worker roster（含角色能力描述）、工作流、完成标记和技能目录渲染编排者的 system prompt
5. 最后启动编排者 agent
6. 将 dispatch loop 作为后台 asyncio 任务 spawn 出来
7. 编排者读取自己的 system prompt，几秒钟内发出第一个 `<<DISPATCH ...>>`

### 观察和介入

每个 AgentPane 用 `RichLog` widget 显示对应 worker 的实时输出，带 ANSI 颜色渲染。四种交互方式：

- **只看**：只看预览滚动
- **快捷按键盘**：点 `Continue`、`Y`、`N`、`Esc`、`^C`、`↑`、`↓`、`^D` 发送一次性按键
- **实时打字**：点输入框（`⌨ click here to type to agent`）开始打字。每个字符立即被转发。
- **完全 attach**：点预览区聚焦它，然后按 **Enter**，或者点 header 里的 **Enter** 按钮。你的终端会切到 agent 的 tmux pane 进行完全原生的交互。按 **Ctrl+B D** 脱离并返回 TUI。

当编排者发出 `<<WORKFLOW_COMPLETE>>` 时，弹 toast 提示，dispatch loop 退出。如果编排者发出 `<<WORKFLOW_ABORT reason="..."/>>`，原因会显示在 toast 里。

---

## 交互模型

Agent Orchestra 在每个 pane 上提供**四种独立的交互模式**，每一种针对不同的用例。v1 的"输入一条消息然后点 Send"模型已经移除 —— 它无法处理 Claude CLI 的交互式提示、方向键菜单或 Tab 补全。

### 1. 只读预览（看）

一个 Textual `RichLog` widget，显示 worker pane 的实时 `tmux capture-pane -p -e` 输出。`rich.text.Text.from_ansi` 把 ANSI 转义序列转成颜色 / 粗体 / 暗体渲染。

**滚动锁**：视图默认自动跟随底部。你手动向上滚动时，自动跟随会暂停，并出现一个 `↓ jump to latest` 按钮。点它（或在聚焦时按 `End`）跳到底部并恢复自动跟随。

### 2. 快捷按键盘（一键）

预览下方一排 8 个按钮：

| 按钮 | 发送 |
|:---|:---|
| `Continue` | `continue\n`（Claude CLI 的"继续"命令） |
| `Y` | `y\n`（确认 `[Y/n]` 提示） |
| `N` | `n\n` |
| `Esc` | 真正的 Escape 按键（取消菜单） |
| `^C` | Ctrl+C（中断） |
| `↑` | 上方向键（选项菜单导航） |
| `↓` | 下方向键 |
| `^D` | Ctrl+D（EOF / 退出） |

每次点击产生一次 `tmux send-keys` 调用。

### 3. 专用输入框（实时打字）

每个 AgentPane 的最后一行是一个可聚焦的 `Static` widget，标签为 `⌨ click here to type to agent (double-Esc to leave)`。点它开始打字：

- **每一次按键**都被 `frontend.key_forwarding.tmux_args_for_key(event)` 映射成 tmux key 规范，立刻通过 `SessionManager.send_raw_keys` 转发。没有批量提交，没有"写完再发"。
- **标点生效。** helper 对 printable 输入优先用 `event.character`，所以 Textual 把 Shift 组合键报告为 `key="exclamation_mark"` 的情况下，`!` 仍然能到达 agent。
- **Tab、方向键、Ctrl+ 组合键、Enter** 全都转发。Tab 不会移动 TUI 的焦点。
- **双击 Esc** 离开输入框。单次 Esc 被转发给 agent（Claude CLI 用它取消菜单）。
- **本地 echo**：打出的 printable 字符也会显示在 widget 的 label 上，提供即时的视觉反馈。

### 4. 完全 Attach（逃生出口）

当你需要真家伙 —— Tab 补全、粘贴、鼠标选择、ANSI 光标应用、长篇组合输入 —— 在 AgentPane 的预览聚焦时按 **Enter**，或点 header 里的 **Enter** 按钮。底层调用的是 `tmux_attach.grouped_attach`（你从 tmux 里启动 TUI 时）或 `suspend_attach`（你在 tmux 外时）。

两条路都会让你落到 worker 的 pane 里进行完全原生交互。按 **Ctrl+B D** 脱离并返回 TUI。pane 状态被保留；TUI 恢复轮询。

---

## Orchestrator 协议

这是编排者 agent 和平台之间的 wire protocol。**完全是 pane 文本** —— 没有 RPC、没有 MCP、没有 JSON 块。

### Dispatch —— 编排者让 worker 做事

编排者发出一行形如：

```
<<DISPATCH role="developer" text="请调用 /req-3-code 技能，目标是：实现缓存层。完成后输出 <<TASK_DONE>>。">>
```

- `role` 必须是合法的 worker 角色名（`product_manager`、`tech_director`、`developer`、`tester`、`user`）—— 不能是 `orchestrator`
- `text` 是要发给 worker 的完整 prompt；支持 `\"` 转义引号
- **仅支持自闭合形式。** 早期版本要求 `<</DISPATCH>>` 闭合标签；REQ-016 F-04a 放弃这个要求，因为 LLM 经常忘记或变异闭合标签
- `text` 不能包含 `<<TASK_DONE>>`、`<<WORKFLOW_COMPLETE>>` 或 `<<WORKFLOW_ABORT` —— 这些是平台保留标记
- `text` 不应包含换行；如果包含，dispatch_loop 会在转发前把换行替换成空格

### Completion —— worker 发信号表示完成

worker 输出它的产出，最后一行是：

```
<<TASK_DONE>>
```

supervisor 的 dispatch_loop 每 500ms 通过 `tmux capture-pane` 轮询一次，用行首 regex 匹配检测这个标记。

**三层 completion** 按优先级尝试：

| 层 | 触发条件 | 产出质量 |
|:---|:---|:---|
| **marker** | 检测到 `<<TASK_DONE>>` 行 | 干净 —— 从标记处之前提取 |
| **silence** | worker pane 60 秒无新输出且有内容 | 可能不完整；结果块带 `via="silence"` 标志 |
| **stall** | dispatch 10 分钟内没有任何完成信号 | 需要操作员介入 —— TUI 弹出 Force Advance / Abort Workflow toast |

### Worker result —— 平台把产出回传给编排者

```
[WORKER_RESULT role="developer" via="marker"]
<worker 在标记之前产出的所有内容，原样>
[/WORKER_RESULT]
```

这个块被通过 `send_keys` 注入到编排者的 pane 里。编排者接着决定下一次 dispatch。

### 工作流完成和中止

当编排者判断工作流完成：

```
<<WORKFLOW_COMPLETE>>
```

当遇到不可恢复的情况：

```
<<WORKFLOW_ABORT reason="测试连续 3 次失败"/>>
```

两者都会终结 dispatch loop 并向 TUI post 一条 Textual 消息。

### 平台 → 编排者 的错误反馈

如果编排者做错了什么，平台会注入以下其中一种消息：

- `[PLATFORM_ERROR: unknown role 'marketing' — valid roles: developer, pm, ...]`
- `[PLATFORM_ERROR: dispatch text must not contain '<<TASK_DONE>>']`
- `[WORKER_ERROR role="developer" reason="pane vanished"]`
- `[PLATFORM_STALL: no completion signal from role="developer" after 600 seconds]`

编排者把这些当作普通文本看到，并被期望自己恢复（重试、跳过、中止 —— 它自己决定）。

### Tester 失败循环

像 `standard` 这样的工作流在 Tester 步骤上携带一个 `failure_loop_to`。当 Tester 的产出除了 `<<TASK_DONE>>` 之外还包含 `<<TESTS_FAILED>>` 时，supervisor 递增一个重试计数器，编排者的下一次 dispatch 通常应该回到 Developer。计数器是信息性的；最终由编排者决定是否重试、跳过或中止。

---

## 内置工作流

`backend/workflows.py` 内置三个剧本。编排者把它们当作**典型流程**而不是严格的状态机 —— 跳过、重复、重排序都被明确允许。

### `standard`

完整的需求到验收流水线：

```
1. Product Manager   — 产出完整的需求规格说明。
2. Tech Director     — 审阅规格并产出技术设计。
3. Developer         — 实现技术设计。
4. Tester            — 运行测试套件并报告结果。
                       如果出现 <<TESTS_FAILED>>，回到步骤 3（最多 3 次重试）。
5. User              — 由人类（或人类替身）用户做验收复审。
```

### `prototype`

两步剧本，用于快速实验：

```
1. Developer         — 实现原型。
2. User              — 原型验收复审。
```

### `research`

设计专用剧本，没有编码阶段：

```
1. Product Manager   — 框定研究问题和期望的产出。
2. Tech Director     — 调研并产出技术发现文档。
3. User              — 对发现的验收复审。
```

---

## 技能目录

编排者在自己的 system prompt 里有一份 `/req-*` 技能目录，它可以要求任何 worker 调用其中的一个。目录是纯数据（`workflows.AVAILABLE_SKILLS`）；新增一个技能只需要追加一行 tuple。

| 技能 | 用途 |
|:---|:---|
| `/req-1-analyze` | 把简短描述扩展成一份完整的需求文档（requirement.md），包含背景、功能需求、验收标准、变更日志 |
| `/req-2-tech` | 基于已定稿的需求产出技术设计（technical.md）：技术栈、架构、模块设计、数据模型、关键流程、风险 |
| `/req-3-code` | 按技术设计实现代码：高内聚低耦合模块、日志、注释、自动化脚本 |
| `/req-4-security` | 代码安全审查：注入攻击、数据泄漏、认证问题、配置漏洞 |
| `/req-5-cleanup` | 结构性清理：检测未使用代码、死代码、重复逻辑；优化内聚/耦合但不改变业务逻辑 |
| `/req-6-review` | 逐条对照需求文档检查实现；标记未声明的修改 |
| `/req-7-verify` | 验证：构建检查、运行时检查、自动化测试、生成验证脚本 |
| `/req-8-done` | 最终归档：一致性检查，把 index.md 状态更新为 Completed |

**关键：技能选择是编排者 LLM 的运行时决策。** workflow dataclass 和 Python 代码里都没有"PM 总是运行 /req-1"这样的映射。编排者读取目录，观察当前情况，逐次 dispatch 决定让 worker 调用哪个（如果有）技能。REQ-017 专门撤回了一次早期尝试把技能 ↔ 步骤映射硬编码进去的改动，完整的理由在 `requirements/REQ-017-restore-orchestrator-autonomy/requirement.md` 里。

---

## 键盘快捷键

### 全局（TUI 级别）

| 键 | 动作 |
|:---|:---|
| `n` | 新建 Agent |
| `g` | 新建 Group |
| `t` | 角色模板编辑器 |
| `z` | 调试 shell（打开一个 zsh pane） |
| `c` | 全部清除（停止所有 session，清空事件日志） |
| `q` | 退出 |

### AgentPane 级别

| 按键 / 动作 | 效果 |
|:---|:---|
| 点 header 里的 `⋯` | 切换 Pause/Resume/Edit/Restart/Delete 管理行的可见性 |
| 点 header 里的 `Enter` **或者** 聚焦预览后按 Enter | Attach 到 worker 的 tmux pane |
| 点只读预览区 | 聚焦预览（用于滚动 / Enter-attach） |
| 点输入框 | 进入按键转发模式 |
| 输入框内单次 `Esc` | 转发到 agent（取消 Claude CLI 菜单） |
| 输入框内双击 `Esc` | 离开输入框（焦点返回 pane 容器） |
| 聚焦预览后按 End | 跳到底部并重新启用自动跟随 |

### 完全 Attach 之后

| 键 | 效果 |
|:---|:---|
| `Ctrl+B D` | 从 worker pane 脱离并返回 TUI |

---

## 配置

所有可调项都在 `src/agent_management/shared/config.py` 里。主要的覆盖方式是在启动前设置环境变量。

### 环境变量

| 变量 | 默认值 | 用途 |
|:---|:---|:---|
| `AGENT_MGMT_DATA_DIR` | `<cwd>/.agent_management/` | SQLite 数据库、临时文件、日志的目录 |

### 代码级可调常量

| 常量 | 默认值 | 用途 |
|:---|:---|:---|
| `CLAUDE_CMD` | `["claude", "--dangerously-skip-permissions"]` | Claude CLI 启动基础命令 |
| `TMUX_SESSION_PREFIX` | `"agent-mgmt"` | 每个 group 对应的 tmux session 前缀 |
| `SESSION_START_TIMEOUT` | `30.0` s | 等待 Claude CLI pane 产生输出的最长时间（readiness 轮询） |
| `SESSION_STOP_TIMEOUT` | `5.0` s | `kill-pane` 之前的优雅关闭窗口期 |
| `DISPATCH_POLL_INTERVAL` | `0.5` s | supervisor 轮询编排者 / worker pane 的频率 |
| `WORKER_SILENCE_TIMEOUT` | `60.0` s | silence 层 completion 超时 |
| `ORCHESTRATOR_STALL_TIMEOUT` | `600.0` s | stall 层超时（触发操作员 toast） |
| `PANE_REFRESH_INTERVAL` | `0.25` s | 只读预览刷新频率（4 Hz） |
| `OUTPUT_BUFFER_LINES` | `500` | `RichLog` 环形缓冲区容量 |
| `DIRECT_SEND_MAX_LEN` | `200` | 历史遗留常量（REQ-016 移除了分支；保留以兼容导入） |
| `SCHEMA_VERSION` | `5` | 破坏性 schema 变更时递增；触发重置模态框 |

### 检查解析后的配置

```bash
uv run python -m agent_management --show-config
```

---

## 数据目录结构

所有运行时状态在 `AGENT_MGMT_DATA_DIR` 下（默认 `<project>/.agent_management/`）：

```
.agent_management/
├── state.db              # SQLite — groups、agents、sessions、role_templates、meta
├── platform.log          # stdlib logging 输出
└── tmp/                  # 临时文件
    ├── orch_prompt_*.txt # 渲染后的编排者 prompt（30 秒后自动清理）
    └── agent_msg_*.txt   # 旧的长 payload 发送辅助文件（已不再使用）
```

### SQLite 架构（v5）

```
agents         — id, name, role, working_dir, system_prompt,
                  system_prompt_file, paused, status, created_at, updated_at
groups         — id, name, workflow_id, created_at
group_members  — group_id, agent_id （多对多）
sessions       — id, agent_id, group_id, claude_session_id,
                  previous_session_id, tmux_session_name, tmux_pane_id,
                  status, started_at, stopped_at
role_templates — role, display_name, system_prompt
meta           — key/value （schema_version、template_version）
```

**破坏性迁移策略**：app 启动时比对 `meta.schema_version`。如果和当前 `SCHEMA_VERSION` 常量不匹配，弹出模态框给用户两个选择：擦掉 `.agent_management/` 继续，或退出。没有迁移脚本 —— 这是个本地单用户工具，迁移的复杂度不值得付出。

---

## 开发

### 运行测试套件

```bash
uv run pytest -q
```

当前数据（截至 REQ-018）：**470 个测试在 ~9 秒内通过**。

测试文件：

| 文件 | 焦点 |
|:---|:---|
| `tests/test_orchestrator.py` | Dispatch 解析器、completion 检测、工作流标记 regex |
| `tests/test_workflows.py` | 内置工作流结构、AVAILABLE_SKILLS 目录、ROLE_CAPABILITIES、渲染 helper |
| `tests/test_repository.py` | SQLite schema、CRUD、角色模板完整性、schema 不匹配检测 |
| `tests/test_models.py` | 领域 dataclass 默认值和不变式 |
| `tests/test_session_manager.py` | Payload sanitizer、编排者 prompt 渲染 |
| `tests/test_supervisor_concurrency.py` | start/stop/resume/clear_group 的并发证明 |
| `tests/test_dispatch_integration.py` | 用 FakeSessionManager 做端到端 dispatch loop —— happy path、silence 层、stall 层、worker 崩溃、scrollback 弹性 |
| `tests/test_key_forwarding.py` | 详尽的 Textual key → tmux argv 映射 |
| `tests/test_agent_pane.py` | 通过 `App.run_test()` pilot 做的 Textual widget 测试 —— admin 切换、快捷按键盘、InputBox 转发 |
| `tests/test_tmux_attach.py` | F-03 attach 路径（grouped / suspend / 环境检测 / 陈旧 session 清理） |

集成测试使用一个镜像真实 `SessionManager` 公共 API 的 `FakeSessionManager`，记录 `send_keys` 调用并重放脚本化的 `capture_pane_full` 响应。测试过程中不启动任何 tmux 或子进程。

### 添加新工作流

编辑 `backend/workflows.py`。定义一个新的 `Workflow` 字面量，包含 `Step(...)` 条目，添加到 `BUILT_IN_WORKFLOWS`。编排者模板会在下次启动时通过 `{{WORKFLOW_DEFINITION}}` placeholder 自动拾起。其他代码无需改动。如果你同时改了编排者 prompt，别忘了 bump `repository.py` 里的 `_TEMPLATE_VERSION` —— force-update 是新模板进入已有 `.agent_management/` 状态的方式。

### 添加新的 `/req-*` 技能

编辑 `backend/workflows.py` → `AVAILABLE_SKILLS` —— 追加一个 `(name, description)` tuple。编排者的 prompt 会通过 `render_skill_catalogue()` 自动渲染它。技能本身是一个 Claude Code CLI 技能目录，放在 `.claude/skills/` 下 —— 那是一个独立的 submodule（`my-skills`）。

### 添加一个角色

1. 在 `backend/models.py` 的 `AgentRole` 里添加 enum 值
2. 在 `repository._DEFAULT_TEMPLATES` 里添加默认模板
3. 在 `workflows.ROLE_CAPABILITIES` 里添加能力描述（REQ-018）
4. bump `_TEMPLATE_VERSION`
5. 按需更新测试

### 需求驱动开发（REQ-driven）

这个项目本身是用 `/req` 技能套件开发的。每一个有意义的改动都要走一个 8 阶段流水线（`分析 → 技术设计 → 编码 → 安全 → 清理 → 需求复审 → 验证 → 归档`）。完整的需求文档历史在 `requirements/` 下：

```
requirements/
├── index.md
├── REQ-001-agent-management-platform/
├── REQ-002-grid-layout/
├── ...
└── REQ-018-orchestrator-centric-refinement/
    ├── requirement.md   # 我们做了什么 + 为什么
    ├── technical.md     # 我们怎么做的
    └── *.puml / *.svg   # PlantUML 图（如果生成了的话）
```

按顺序读 REQ 文档是理解当前架构的来龙去脉最快的方式。

---

## 项目历史（REQ 驱动）

| REQ | 状态 | 摘要 |
|:---|:---|:---|
| REQ-001 | Completed | 最初的 agent 管理平台 —— 多 agent Claude CLI 编排 TUI，带 pub/sub 和 session resume |
| REQ-002 | Completed | 2 列网格布局 + session-ID 修复 |
| REQ-003 | Completed | 可配置数据目录（`AGENT_MGMT_DATA_DIR`） |
| REQ-004 | Completed | 路径输入自动补全 |
| REQ-005 | Completed | CLI `--help` 和 `--show-config` 标志 |
| REQ-006 | Completed | Tech Director 角色 enum 值 |
| REQ-007 | Completed | 可编辑的角色模板、`t` 快捷键、角色选择时自动填充 |
| REQ-008 | Completed | AgentPane 聚焦输入 + 自适应布局 |
| REQ-009 | Completed | Group 自动创建 agent |
| REQ-010 | Completed | Delete group / agent 级联删除 |
| REQ-011 | Completed | 原生 tmux attach / detach |
| REQ-012 v1 | Superseded | 对 MCP 事件总线架构的原始三 bug 补丁 |
| **REQ-012 v2** | **Completed** | **架构转向：删除 MCP 事件总线；引入 LLM Orchestrator 模型、`<<DISPATCH>>` 协议、3 个内置工作流、3 层 completion 检测** |
| REQ-013 | Superseded by REQ-015 | 终端 attach 交互和滚动修复的原始设计 |
| REQ-014 | Completed | REQ-012 v2 质量加固：scrollback bug、临时文件泄漏、pane 崩溃检测、测试套件扩展（90 → 189） |
| REQ-015 | Completed | Native-first 交互：删除旧输入行；带滚动锁的 ANSI 渲染预览；8 键快捷键盘；纯按键转发专用输入框；聚焦预览按 Enter 触发 Attach |
| REQ-016 | Completed | 5 问题打磨：可折叠 admin 行、标点符号转发修复、start/stop/resume 并发、dispatch 可靠性（自闭合解析器、cat fallback 移除、换行规范化、诊断日志） |
| REQ-017 | Completed | 恢复编排者自主权 —— 撤回 REQ-016 的每步技能硬编码；引入 AVAILABLE_SKILLS 目录；编排者模板强调自主决策；worker 模板完全隐藏编排者抽象 |
| **REQ-018** | **Completed** | **Orchestrator-centric 精修：`clear_all` 并发化（对齐 start/stop/resume 的 REQ-016 F-03 模式）；新增 `ROLE_CAPABILITIES` 字典 + 在 `render_roster` 中渲染能力说明，让编排者看到每个下属的能力描述；supervisor 模块文档重写，显式陈述 orchestrator-as-parent 心智模型；完整的中文 README.zh-CN.md 翻译** |

---

## 已知限制

- **一次只能有一个 active group。** `Supervisor._active_group_id` 是单值字段。要并发运行两个 group 需要开另一个 TUI 进程。
- **模板自定义会被版本 bump 覆盖。** 当 `_TEMPLATE_VERSION` 递增时，内置角色模板会被强制覆盖。自定义过模板的用户需要重新应用修改。
- **没有迁移脚本。** schema 版本不匹配会触发破坏性重置。
- **6-pane 布局假设终端足够宽。** 建议 ≥ 160 列。
- **编排者 dispatch 是顺序的。** 同一时间只有一个 worker 在运行；并行 dispatch 不在范围内。
- **多行 dispatch text 被折叠。** dispatch_loop 在 `send_keys` 之前把 `\n` 替换成空格以避免 tmux 提前提交。
- **不支持非 Claude CLI 的 worker。** 所有 pane 都跑 `claude --dangerously-skip-permissions`；没有运行时抽象层来换内核。
- **没有实现 `textual-terminal` 风格的内嵌终端。** 原生交互通过 (a) 按键转发输入框实现实时打字，(b) 完全的 tmux attach 覆盖其他情况。REQ-015 评估过真正的内嵌 VT100 widget 并判定改动过大而拒绝。
- **Windows 上 tmux 必须能从 Git Bash 或 WSL 访问。** 平台通过 `asyncio.create_subprocess_exec` 调用 `tmux` —— 如果 tmux 不在 `PATH` 上，什么都不工作。

---

*Agent Orchestra 是用它自己开发的，dogfood 风格。如果你在仓库里读到这些，README、需求文档、代码和测试都是由人类操作员通过 `/req` 流水线驱动的 Claude Code agent 产出的。*
