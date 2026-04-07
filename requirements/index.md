# Requirement Index

<!-- archive-threshold: 5 -->

## Active

| ID | Name | Status | Updated | Description |
|:---|:---|:---|:---|:---|
| REQ-001 | Agent Management Platform | Completed | 2026-04-07 | Local TUI tool to orchestrate multiple Claude Code CLI agents with pub/sub inter-agent communication, group management, session resume, and per-agent system context |
| REQ-002 | Grid Layout + Session-ID Fix | Completed | 2026-04-07 | Agent panes arranged in 2-column grid; fix --session-id + --resume CLI conflict |
| REQ-003 | Configurable Data Directory | Completed | 2026-04-07 | Data stored in project .agent_management/ by default; override via AGENT_MGMT_DATA_DIR env var |
| REQ-004 | Path Input Autocomplete | Completed | 2026-04-07 | Working directory input suggests matching filesystem paths as you type; Tab to accept |
| REQ-005 | CLI Help & Config Display | Completed | 2026-04-07 | --help and --show-config flags print usage and resolved runtime values |
| REQ-006 | Tech Director Role | Completed | 2026-04-07 | Add tech_director AgentRole enum value |
| REQ-007 | Role Templates | Completed | 2026-04-07 | Editable default system prompts per role; t keybinding to manage; auto-fill on role select; user role added |
| REQ-008 | AgentPane Focus Input + Layout | Completed | 2026-04-07 | Input row shows only when pane is focused; AgentPane height adaptive to fill available space |
| REQ-009 | Group Auto-Create Agents | Completed | 2026-04-07 | Creating a group auto-creates PM/Tech Dir/Dev/Tester/User agents named "{group} - {role}", with shared working directory |
| REQ-010 | Delete Group & Agent | Completed | 2026-04-07 | Delete button on AgentPane and GroupPanel; confirmation dialog before deletion; cascade-deletes agents when group is deleted |
| REQ-011 | Native tmux Attach / Detach | Completed | 2026-04-07 | Enter button on AgentPane to attach user's terminal to agent's live tmux pane for native keyboard interaction; Ctrl+B D to return; MCP SSE reconnection on resume |
| REQ-012 | Replace MCP Event Bus with LLM Orchestrator | Completed | 2026-04-08 | v2 pivot: replace MCP event bus with a 6th orchestrator agent that drives a workflow (standard / prototype / research) by dispatching to PM/Tech Director/Dev/Tester/User via tmux send-keys. Completion detection: `<<TASK_DONE>>` marker + 60s silence + 10min stall fallback. Deletes mcp_server.py, pending_events/events tables, topic_list/auto_respond columns. Retains v1 F-03 (Enter pane routing), F-04 (readiness poll), F-06 (error toasts). Requires destructive schema reset. |
| REQ-013 | Terminal Attach Interaction & Output Scroll Fix | Requirement Finalized | 2026-04-07 | Make Enter/Attach the primary interaction model (native terminal experience); fix output area scrollbar bug (auto-scroll locks user at bottom); add quick-action buttons. Depends on REQ-012. |
| REQ-014 | Quality Hardening for REQ-012 v2 | Completed | 2026-04-08 | Post-hoc code review and test expansion: fixed scrollback offset wraparound in dispatch loop (content-signature dedup), orchestrator tmp-prompt file leak, worker-pane crash vs silence timeout, tester failure retry soft cap; removed dead PaneOutputRefresh and _check_mcp_alive code. Added ~99 tests: 34 orchestrator, 11 workflows, 16 repository, 6 models, 19 session_manager (new file), 13 dispatch_loop integration (new file with FakeSessionManager). Total: 90 → 189 tests. |

## Archived

| ID | Name | Completed | Description |
|:---|:---|:---|:---|
