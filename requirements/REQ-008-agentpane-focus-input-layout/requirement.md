# REQ-008: AgentPane Focus-Driven Input & Layout Optimization

## Status: RequirementDraft
## Created: 2026-04-07
## Author: system

---

## Background

The Agent Management Platform TUI displays multiple `AgentPane` widgets in a 2-column grid. Each pane shows live tmux output and controls. A text input row was recently added (REQ-008 predecessor) allowing users to send messages directly to agents.

Two UI problems remain:

1. **Input row always visible**: Every pane shows its input row at all times, creating visual noise and consuming vertical space even for agents the user isn't interacting with.
2. **Insufficient content area**: The `RichLog` (tmux output) occupies too little vertical space. Fixed-height header + controls + input rows (9 lines total) leave only ~11 lines for content in a 20-line pane.

---

## Target Users

Local developers running the Agent Management Platform on macOS terminal.

---

## Functional Requirements

### F-01: Focus-Driven Input Visibility

| ID | Requirement |
|:---|:---|
| F-01-1 | Each `AgentPane`'s input row (`pane-input`) MUST be hidden by default on mount |
| F-01-2 | When a user clicks or focuses any element inside an `AgentPane`, the input row for that pane MUST become visible |
| F-01-3 | When focus leaves an `AgentPane` (to another pane or TUI area), the input row MUST be hidden again |
| F-01-4 | Only one pane's input row is visible at any time |
| F-01-5 | When the input row hides, it MUST collapse to zero height (no blank space) |

### F-02: Layout Height Optimization

| ID | Requirement |
|:---|:---|
| F-02-1 | `AgentPane` height MUST be adaptive (`1fr`) rather than fixed pixels, growing to fill the grid cell |
| F-02-2 | A `min-height` guard MUST prevent panes from collapsing below a usable minimum (≥ 16 lines) |
| F-02-3 | `RichLog` (content area) MUST use `height: 1fr` to absorb all available vertical space within the pane |
| F-02-4 | Header, controls, and input rows MUST use fixed compact heights (≤ 3 lines each) |

---

## Non-Functional Requirements

| ID | Requirement |
|:---|:---|
| NF-01 | Focus/blur transitions MUST be instantaneous (no animation delay) |
| NF-02 | Layout change MUST NOT cause horizontal scrollbars or content clipping |
| NF-03 | Changes MUST NOT affect AgentPane functionality (send, pause, resume, edit, restart) |

---

## Use Cases

### UC-01: User focuses an agent pane

**Actor:** User
**Precondition:** TUI running, at least one AgentPane visible
**Main Flow:**
1. User clicks anywhere inside AgentPane A (header, log area, or control buttons)
2. AgentPane A receives descendant focus event
3. Input row of AgentPane A becomes visible
4. If AgentPane B previously had visible input row, it is now hidden

**Alternate Flow — keyboard navigation:**
1. User tabs into AgentPane A's input field
2. Same result as step 2-4 above

---

### UC-02: User sends message to focused agent

**Actor:** User
**Precondition:** AgentPane A is focused, input row visible
**Main Flow:**
1. User types a message in the input field
2. User presses Enter (or clicks Send)
3. Message is sent to the agent's tmux pane
4. Input field clears
5. Input row remains visible (pane still focused)

---

### UC-03: User clicks elsewhere to defocus

**Actor:** User
**Precondition:** AgentPane A is focused, input row visible
**Main Flow:**
1. User clicks on GroupPanel, EventLog, or another AgentPane
2. AgentPane A receives blur event
3. Input row of AgentPane A hides

---

## Acceptance Criteria

| AC | Description |
|:---|:---|
| AC-01 | On startup, no AgentPane shows an input row |
| AC-02 | Clicking an AgentPane's RichLog area shows that pane's input row |
| AC-03 | Clicking a different AgentPane hides the first pane's input row and shows the new one's |
| AC-04 | Input row collapses to zero height when hidden (no blank line) |
| AC-05 | AgentPane height grows to fill available space (not fixed at 20 lines) |
| AC-06 | RichLog occupies the majority of each AgentPane's height |
| AC-07 | Send / Pause / Resume / Edit / Restart buttons continue to work correctly |

---

## Out of Scope

- Keyboard-driven pane navigation (Tab between panes)
- Focus indicator styling (border highlight etc.)
- ShellPane focus-driven input (separate concern)

---

## Change Log

| Version | Date | Author | Changes |
|:---|:---|:---|:---|
| v1 | 2026-04-07 | system | Initial draft |
