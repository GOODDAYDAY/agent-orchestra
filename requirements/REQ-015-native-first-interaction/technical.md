# REQ-015 Technical Design — Native-First Interaction

> Status: Completed
> Requirement: requirement.md
> Created: 2026-04-08
> Updated: 2026-04-08

## 1. Technology Stack

| Module | Technology | Rationale |
|:---|:---|:---|
| Read-only preview | `textual.widgets.RichLog` (already used) + `rich.text.Text.from_ansi` | Rich already understands ANSI escape sequences via `Text.from_ansi`; zero new deps |
| Scroll lock | RichLog `scroll_y` / `is_vertical_scroll_end` introspection + `auto_scroll=False` + manual scroll-to-end | Textual exposes the scroll state we need without subclassing |
| Button keyboard | `textual.widgets.Button` rows | Native Textual primitives |
| Dedicated input box | Custom focusable `Static` subclass with `on_key` override | A `Static` widget can be made focusable; we don't want the `Input` widget's editing behaviour |
| Key forwarding mapping | Pure-function module `frontend/key_forwarding.py` | Trivially unit-testable, no dependencies |
| Tmux raw key send | New `SessionManager.send_raw_keys` method | Distinct from `send_keys` (which appends Enter and sanitises text) — required because key specs are tmux verbs, not text payloads |
| ANSI capture | `tmux capture-pane -p -e` flag | Native tmux feature, no parsing needed |

## 2. Design Principles

- **Four interaction modes, one widget tree**: read-only preview, button keyboard, input box (focus-catcher with key forwarding), and Attach. Each mode has a clean home in the AgentPane layout — no overlapping responsibilities.
- **Pure-function key mapping**: `key_forwarding.textual_to_tmux` is side-effect free, fully unit-testable, and trivially extensible (add a row to the table when a new key is needed).
- **Two send paths**: `SessionManager.send_keys` (existing) sends sanitised text + Enter and is the orchestrator dispatch path. `SessionManager.send_raw_keys` (new) sends literal tmux key specs and is the user-input path. The two never share state.
- **No new dependencies**: ANSI rendering uses Rich's `Text.from_ansi`. The dedicated input box is a focusable `Static`, not a third-party terminal widget.
- **Backwards-compatible at the API surface**: `capture_pane_full(pane_id)` keeps the same default behaviour; `ansi=True` is opt-in. Tests for the supervisor don't change.

## 3. Architecture Overview

```
┌────────────────────── AgentPane ───────────────────────┐
│  Header: name | status | role-marker                   │ ← admin row, unchanged
│ ┌────────────────────────────────────────────────────┐ │
│ │ Read-only preview (RichLog)                        │ │ ← F-02 ANSI render + scroll lock
│ │ - capture-pane -p -e -S -2000                      │ │
│ │ - rich.text.Text.from_ansi conversion              │ │
│ │ - auto_scroll=False, manual scroll detection       │ │
│ │ - Enter on focus → AttachRequested (F-06)          │ │
│ └────────────────────────────────────────────────────┘ │
│ [↓ jump to latest]   ← visible only when scrolled up    │
│ ┌────────────────────────────────────────────────────┐ │
│ │ Pause Resume Edit Restart Delete Enter            │ │ ← admin controls (existing)
│ └────────────────────────────────────────────────────┘ │
│ ┌────────────────────────────────────────────────────┐ │
│ │ Continue  Y  N  Esc  ^C  ↑  ↓  ^D                 │ │ ← F-03 button keyboard
│ └────────────────────────────────────────────────────┘ │
│ ┌────────────────────────────────────────────────────┐ │
│ │ ⌨ click here to type to agent (Esc to leave)       │ │ ← F-04 input box / focus catcher
│ └────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

Wiring:

```
AgentPane.PreviewLog (focused)  ──Enter──▶  AgentPane.AttachRequested ──▶ App._handle_attach
AgentPane.PreviewLog (focused)  ──End ───▶  scroll to bottom + re-enable auto-follow
AgentPane.JumpButton clicked    ─────────▶  scroll to bottom + re-enable auto-follow

AgentPane.QuickKeyButton clicked
                           │
                           ▼
        SessionManager.send_raw_keys(pane_id, *spec)
                           │
                           ▼
        tmux send-keys -t {pane} <spec>

AgentPane.InputBox (focused)  ──any key──▶  key_forwarding.textual_to_tmux(event.key)
                                                    │
                                                    ▼
                                  SessionManager.send_raw_keys(pane_id, *args)
                                                    │
                                                    ▼
                                  tmux send-keys -t {pane} <args>

App._refresh_pane_outputs ──▶ SessionManager.capture_pane_full(pane_id, ansi=True)
                                                    │
                                                    ▼
                                  AgentPane.set_output(text)  ──▶ Text.from_ansi
                                                    │
                                                    ▼
                                  RichLog.write + scroll-lock check
```

Modules touched:

```
backend/session_manager.py         ✏️  add send_raw_keys; capture_pane_full ansi flag
shared/config.py                   ✏️  add OUTPUT_POLL_INTERVAL_MS, OUTPUT_BUFFER_LINES
frontend/key_forwarding.py         ✨  NEW pure-function key map
frontend/agent_pane.py             🔥  rewrite layout: drop Input/Send, add preview ANSI rendering
                                       + scroll lock + button keyboard + input box focus catcher
frontend/app.py                    ✏️  drop on_agent_pane_send_requested handler;
                                       _refresh_pane_outputs uses ansi=True
requirements/index.md              ✏️  REQ-013 → Superseded by REQ-015; REQ-015 → Completed
requirements/REQ-013-*/requirement.md  ✏️  prepend supersession note
tests/test_key_forwarding.py       ✨  NEW exhaustive mapping table coverage
tests/test_session_manager.py      ✏️  add send_raw_keys + capture_pane_full(ansi=True) tests
tests/test_agent_pane.py           ✨  NEW focus state, button keyboard wiring, scroll lock,
                                       input box key forwarding interaction
```

## 4. Module Design

### 4.1 `frontend/key_forwarding.py` (new)

**Public interface:**

```python
from typing import Optional

def textual_to_tmux(event_key: str) -> Optional[list[str]]:
    """Map a Textual events.Key.key string to a tmux send-keys argv list.

    Returns None for unrecognised keys (caller drops the event silently).
    Pure function: no IO, no exceptions on bad input.
    """
```

**Internal table:**

```python
_SPECIAL: dict[str, list[str]] = {
    "enter": ["Enter"],
    "tab": ["Tab"],
    "backspace": ["BSpace"],
    "delete": ["DC"],
    "escape": ["Escape"],
    "up": ["Up"],
    "down": ["Down"],
    "left": ["Left"],
    "right": ["Right"],
    "home": ["Home"],
    "end": ["End"],
    "pageup": ["PPage"],
    "pagedown": ["NPage"],
    "space": ["Space"],
    "insert": ["IC"],
}
# F1..F12 generated programmatically
for i in range(1, 13):
    _SPECIAL[f"f{i}"] = [f"F{i}"]
```

**Algorithm:**

1. If `event_key` is empty → return `None`
2. If `event_key` is in `_SPECIAL` → return that mapping
3. If `event_key` starts with `ctrl+` → letter portion → return `[f"C-{letter}"]`. Special: `ctrl+space` → `["C-Space"]`, `ctrl+]` → `["C-]"]`.
4. If `event_key` is a single printable character → return `[event_key]`
5. If `event_key` is a multi-character unicode string (e.g. from IME composition) → return `[event_key]`
6. Otherwise → return `None`

**Tests** (`tests/test_key_forwarding.py`): one assert per row of the mapping table; explicit assertion for `None` on garbage input; ctrl+letter coverage for all 26 letters; F1..F12; printable ASCII boundary; unicode passthrough.

### 4.2 `backend/session_manager.py` (modified)

**New method `send_raw_keys`:**

```python
async def send_raw_keys(
    self, pane_id: str, *key_args: str
) -> tuple[int, str, str]:
    """Send raw tmux key specs to a pane via `tmux send-keys` (no payload
    sanitisation, no trailing Enter).

    Used by the user-input forwarding path (button keyboard and dedicated
    input box). Distinct from `send_keys`, which sanitises text and appends
    Enter — that path is reserved for the orchestrator dispatch loop.

    Returns (rc, stdout, stderr) so callers can react to failures.
    """
    if not key_args:
        return 0, "", ""
    return await self._tmux("send-keys", "-t", pane_id, *key_args)
```

**Modified `capture_pane_full`:**

```python
async def capture_pane_full(
    self,
    pane_id: str,
    history_lines: int = 2000,
    ansi: bool = False,
) -> str:
    args = ["capture-pane", "-p", "-S", f"-{history_lines}", "-t", pane_id]
    if ansi:
        args.insert(2, "-e")  # -p -e -S ...
    rc, stdout, _ = await self._tmux(*args)
    return stdout if rc == 0 else ""
```

The supervisor's `dispatch_loop` continues to call without `ansi=True`. Only the AgentPane refresh path passes `ansi=True`.

### 4.3 `shared/config.py` (modified)

Add two constants:

```python
# REQ-015: AgentPane preview tuning
OUTPUT_POLL_INTERVAL_MS: int = 500
OUTPUT_BUFFER_LINES: int = 500
```

`PANE_REFRESH_INTERVAL` (the existing 0.25s top-level pane refresh) remains; the new `OUTPUT_POLL_INTERVAL_MS` is reserved for future per-pane finer-grained polling and tests, but in v1 the existing app-level timer drives the refresh.

### 4.4 `frontend/agent_pane.py` (rewritten)

**Layout (`compose`):**

```python
def compose(self) -> ComposeResult:
    # Header (unchanged)
    with Horizontal(classes="pane-header"):
        yield Label(f"[bold]{self.agent.name}[/bold]", id=f"name-{self.agent.id}")
        yield Label(..., id=f"status-{self.agent.id}", classes="status-badge")
        yield Label("", id=f"role-marker-{self.agent.id}", classes="role-marker")

    # Read-only preview — focusable for Enter-attach (F-06)
    log = RichLog(
        id=f"log-{self.agent.id}",
        auto_scroll=False,
        markup=False,
        highlight=False,
        max_lines=OUTPUT_BUFFER_LINES,
    )
    log.can_focus = True
    yield log

    # Jump-to-latest indicator (hidden by default)
    yield Button(
        "↓ jump to latest",
        id=f"btn-jump-{self.agent.id}",
        classes="jump-button hidden",
        compact=True,
    )

    # Admin controls row (unchanged)
    with Horizontal(classes="pane-controls"):
        yield Button("Pause",   id=f"btn-pause-{self.agent.id}",   variant="warning", compact=True)
        yield Button("Resume",  id=f"btn-resume-{self.agent.id}",  variant="success", compact=True)
        yield Button("Edit",    id=f"btn-edit-{self.agent.id}",    variant="default", compact=True)
        yield Button("Restart", id=f"btn-restart-{self.agent.id}", variant="error",   compact=True)
        yield Button("Delete",  id=f"btn-delete-{self.agent.id}",  variant="error",   compact=True)
        yield Button("Enter",   id="enter-agent",                   variant="primary", compact=True)

    # Quick keyboard row (F-03)
    with Horizontal(classes="pane-quickkeys"):
        for label, key_id in QUICK_KEYS:
            yield Button(label, id=f"qk-{key_id}-{self.agent.id}", variant="default", compact=True)

    # Dedicated input box (F-04) — focus catcher
    yield InputBox(agent_id=self.agent.id, id=f"inp-fwd-{self.agent.id}")
```

**`QUICK_KEYS` constant:**

```python
QUICK_KEYS: list[tuple[str, str]] = [
    ("Continue", "continue"),
    ("Y",        "y"),
    ("N",        "n"),
    ("Esc",      "escape"),
    ("^C",       "ctrl-c"),
    ("↑",        "up"),
    ("↓",        "down"),
    ("^D",       "ctrl-d"),
]

# Maps quick-key id to a tmux send-keys argv list
QUICK_KEY_SPECS: dict[str, list[str]] = {
    "continue": ["continue", "Enter"],
    "y":        ["y", "Enter"],
    "n":        ["n", "Enter"],
    "escape":   ["Escape"],
    "ctrl-c":   ["C-c"],
    "up":       ["Up"],
    "down":     ["Down"],
    "ctrl-d":   ["C-d"],
}
```

**`InputBox` widget (new, in agent_pane.py or a separate file):**

```python
class InputBox(Static):
    """Focus-catcher for pure key forwarding to a tmux pane.

    When focused, every Textual Key event is mapped to a tmux send-keys
    argv via key_forwarding.textual_to_tmux and forwarded immediately.
    Unmapped keys are dropped silently.
    """

    DEFAULT_CSS = """
    InputBox {
        height: 1;
        background: $surface-darken-1;
        color: $text-muted;
        padding: 0 1;
    }
    InputBox:focus {
        background: $primary;
        color: $text;
    }
    """

    can_focus = True

    PLACEHOLDER = "⌨ click here to type to agent (double-Esc to leave)"

    def __init__(self, agent_id: str, **kwargs) -> None:
        super().__init__(self.PLACEHOLDER, **kwargs)
        self.agent_id = agent_id
        self._last_key_was_escape = False
        self._echo: str = ""

    async def on_key(self, event: events.Key) -> None:
        from agent_management.frontend.key_forwarding import textual_to_tmux
        spec = textual_to_tmux(event.key)
        if spec is None:
            return  # let it bubble (e.g. function keys, etc.)
        # Stop propagation so Textual's own handlers don't process this
        event.stop()
        event.prevent_default()
        # Double-Esc detection: leave focus
        if event.key == "escape":
            if self._last_key_was_escape:
                self._last_key_was_escape = False
                self.app.set_focus(None)
                self._reset_echo()
                return
            self._last_key_was_escape = True
        else:
            self._last_key_was_escape = False
        # Forward to agent
        self.post_message(self.KeyForwarded(agent_id=self.agent_id, spec=spec))
        # Local echo for printable chars (best-effort visual feedback)
        if len(event.key) == 1 and event.key.isprintable():
            self._echo += event.key
            self.update(f"⌨ {self._echo}")
        elif event.key == "enter":
            self._reset_echo()
        elif event.key == "backspace" and self._echo:
            self._echo = self._echo[:-1]
            self.update(f"⌨ {self._echo}" if self._echo else self.PLACEHOLDER)

    def on_blur(self) -> None:
        self._reset_echo()

    def _reset_echo(self) -> None:
        self._echo = ""
        self.update(self.PLACEHOLDER)

    class KeyForwarded(Message):
        def __init__(self, agent_id: str, spec: list[str]) -> None:
            super().__init__()
            self.agent_id = agent_id
            self.spec = spec
```

**Scroll lock detection:**

`set_output(text)` becomes:

```python
def set_output(self, text: str) -> None:
    log = self.query_one(f"#log-{self.agent.id}", RichLog)
    # Detect whether the user is currently at the bottom (within 1 line)
    at_bottom = log.scroll_y >= log.max_scroll_y - 1
    # Render ANSI escape codes via rich.text.Text
    log.clear()
    if text:
        from rich.text import Text
        log.write(Text.from_ansi(text))
    if at_bottom:
        log.scroll_end(animate=False)
    else:
        # Show the jump-to-latest button
        try:
            jump = self.query_one(f"#btn-jump-{self.agent.id}", Button)
            jump.remove_class("hidden")
        except Exception:
            pass
```

**Enter-on-focused-preview (F-06):**

```python
async def on_key(self, event: events.Key) -> None:
    if event.key != "enter":
        return
    # Only fire if the focused widget is the RichLog, not the input box
    focused = self.app.focused
    if focused is None:
        return
    if focused.id == f"log-{self.agent.id}":
        event.stop()
        self.post_message(self.AttachRequested(agent_id=self.agent.id))
```

**Button click routing (F-03 + Enter button):**

`on_button_pressed` is extended to recognise the new `qk-*` button IDs and post a new `KeyForwarded` message:

```python
def on_button_pressed(self, event: Button.Pressed) -> None:
    bid = event.button.id or ""
    aid = self.agent.id
    # ... existing handlers for pause/resume/edit/restart/delete/enter ...
    # New: jump button
    if bid == f"btn-jump-{aid}":
        log = self.query_one(f"#log-{aid}", RichLog)
        log.scroll_end(animate=False)
        event.button.add_class("hidden")
        return
    # New: quick key buttons
    if bid.startswith(f"qk-") and bid.endswith(f"-{aid}"):
        key_id = bid[len("qk-"):-len(f"-{aid}")]
        spec = QUICK_KEY_SPECS.get(key_id)
        if spec:
            self.post_message(self.KeyForwarded(agent_id=aid, spec=spec))
        return
```

A `KeyForwarded` message is added to AgentPane, mirroring the InputBox.KeyForwarded — both feed into the same app-level handler.

### 4.5 `frontend/app.py` (modified)

- Delete `on_agent_pane_send_requested` (the OLD `SendRequested` handler).
- Add `on_agent_pane_key_forwarded` (and `on_input_box_key_forwarded` if InputBox lives in its own file). Both call `SessionManager.send_raw_keys` with the spec.
- `_refresh_pane_outputs`: change `capture_pane_output(pane_id)` to `capture_pane_full(pane_id, ansi=True)`. Use the existing 4 Hz timer as the refresh driver — `OUTPUT_POLL_INTERVAL_MS` is reserved for future use.
- Drop `SendRequested` import.

### 4.6 REQ-013 supersession

- Edit `requirements/REQ-013-terminal-attach-interaction/requirement.md` to prepend a markdown note:

  ```markdown
  > **Status update**: This requirement is **Superseded by REQ-015 (Native-First Interaction)**.
  > REQ-015 absorbs all of REQ-013's functional requirements (F-01 through F-05) and adds the
  > deletion of the old Input/Send affordance plus a dedicated key-forwarding input box.
  > REQ-013 was never implemented; the document is preserved here as historical analysis.
  ```

- Update `requirements/index.md`: REQ-013 row status `Requirement Finalized` → `Superseded by REQ-015`.

## 5. Data Model

No schema changes.

## 6. API Design

No external API. Internal new APIs:

| API | Module | Notes |
|:---|:---|:---|
| `key_forwarding.textual_to_tmux(key: str) -> Optional[list[str]]` | `frontend/key_forwarding.py` | Pure function |
| `SessionManager.send_raw_keys(pane_id: str, *key_args: str) -> tuple[int, str, str]` | `backend/session_manager.py` | Async, returns tmux command result |
| `SessionManager.capture_pane_full(pane_id: str, history_lines: int = 2000, ansi: bool = False) -> str` | extended | Backward-compatible default |
| `AgentPane.KeyForwarded(agent_id, spec)` | `frontend/agent_pane.py` | Textual message |
| `InputBox.KeyForwarded(agent_id, spec)` | `frontend/agent_pane.py` | Textual message; shape mirrors AgentPane.KeyForwarded |

## 7. Key Flows

### 7.1 Button keyboard click

1. User clicks `Y` on AgentPane for agent X
2. AgentPane.on_button_pressed sees button id `qk-y-{X}`
3. Looks up `QUICK_KEY_SPECS["y"]` → `["y", "Enter"]`
4. Posts `AgentPane.KeyForwarded(agent_id=X, spec=["y", "Enter"])`
5. App.on_agent_pane_key_forwarded resolves the agent's session and pane_id
6. Calls `SessionManager.send_raw_keys(pane_id, "y", "Enter")`
7. Tmux receives `tmux send-keys -t {pane} y Enter`

### 7.2 Input box typing

1. User clicks the InputBox below the button keyboard → focus moves to InputBox
2. InputBox visual changes: `⌨ ` placeholder + bright background
3. User types `g`
4. Textual emits `events.Key(key="g")` to InputBox
5. InputBox.on_key:
   - `textual_to_tmux("g")` → `["g"]`
   - Posts `InputBox.KeyForwarded(agent_id, spec=["g"])`
   - Echoes `g` locally → label becomes `⌨ g`
6. App handler calls `send_raw_keys(pane_id, "g")` → `tmux send-keys -t {pane} g`
7. Agent's tmux pane receives `g`; capture-pane refresh shows it within 250ms
8. User presses Enter → `textual_to_tmux("enter")` → `["Enter"]` → forwarded → echo cleared

### 7.3 Enter on focused preview triggers Attach

1. User clicks the RichLog area → focus moves to the log
2. User presses Enter
3. Textual's key event reaches AgentPane.on_key
4. The focused widget id is `log-{agent.id}` → AgentPane posts `AttachRequested(agent_id)`
5. App._handle_attach runs the existing REQ-011 attach flow

### 7.4 Scroll lock

1. Refresh timer fires; App._refresh_pane_outputs polls capture_pane_full(pane_id, ansi=True)
2. Calls AgentPane.set_output(text)
3. set_output reads `log.scroll_y` and `log.max_scroll_y`
4. If `scroll_y >= max_scroll_y - 1` (at bottom), set_output writes the new content, then scrolls to end → user keeps following
5. If not at bottom, set_output writes the new content but does NOT scroll → user keeps reading history
6. The jump-to-latest button is added when scroll_y < max_scroll_y - 1, removed when at_bottom

## 8. Shared Modules & Reuse Strategy

| Shared | Used by | Notes |
|:---|:---|:---|
| `key_forwarding.textual_to_tmux` | InputBox, future broadcast features | Pure function, fully tested |
| `SessionManager.send_raw_keys` | AgentPane button keyboard, InputBox key forwarding | Distinct from `send_keys` (orchestrator path) |
| `SessionManager.capture_pane_full(ansi=True)` | AgentPane preview only | Default behaviour unchanged |
| `rich.text.Text.from_ansi` | AgentPane.set_output | Already a Rich primitive — no new dep |
| Existing F-03 attach helpers | AgentPane.AttachRequested handler | Unchanged; F-06 just provides another way to trigger them |

## 9. Risks & Notes

| Risk | Mitigation |
|:---|:---|
| Textual Key event names differ across versions | The mapping table is data; new keys are one-line additions. Tests cover the canonical names from the version we use. |
| Pure key forwarding may interfere with Textual's own bindings (Tab to move focus, etc.) | InputBox.on_key calls `event.stop()` and `event.prevent_default()` for every recognised key. Tab is in `_SPECIAL`, so it forwards instead of moving focus — by design. |
| User clicks RichLog and presses Enter — but they meant to scroll, not attach | Documented; two-step keyboard flow (click first, then Enter). The Enter button on the controls row remains as the mouse-friendly affordance. |
| Local echo in InputBox diverges from agent's actual state (e.g. when agent rejects a character) | The echo is purely cosmetic and resets on Enter. The read-only preview is the source of truth. |
| ANSI escape sequences in capture-pane output may include cursor positioning that confuses RichLog | RichLog renders the parsed Rich Text — Text.from_ansi handles colours/styles correctly and silently drops cursor-positioning sequences. Acceptable trade-off (we don't need cursor positioning in a read-only view). |
| Per-character send_raw_keys creates many subprocess calls under fast typing | tmux send-keys is sub-millisecond on a local socket; not a real concern. Future optimisation: batch multiple chars per call if profiling shows it. |
| User accidentally enters InputBox and starts typing into the wrong agent | The visual focus highlight and the placeholder text make the active widget unmistakable. Esc-Esc leaves immediately. |
| RichLog.max_scroll_y may not exist on every Textual version | Use `getattr(log, "max_scroll_y", None)` with a fallback to `log.virtual_size.height - log.size.height`. |

## 10. Test Strategy

| Test file | Scope | Style |
|:---|:---|:---|
| `tests/test_key_forwarding.py` (new) | All special keys, all `ctrl+letter` combos, F1..F12, printable ASCII boundary, unicode passthrough, garbage input returns None | Pure unit tests |
| `tests/test_session_manager.py` (extended) | `send_raw_keys` argv, ansi flag in capture_pane_full | Patch `_tmux` to record args |
| `tests/test_agent_pane.py` (new) | InputBox.on_key dispatches KeyForwarded for known keys; InputBox.on_key drops None; double-Esc leaves focus; QUICK_KEY_SPECS coverage; AgentPane.on_key triggers AttachRequested only when log is focused; scroll-lock detection with synthetic scroll_y values | Textual snapshot-style tests where useful; otherwise direct widget instantiation and event simulation |

## 11. Change Log

| Version | Date | Changes | Affected Scope | Reason |
|:---|:---|:---|:---|:---|
| v1 | 2026-04-08 | Initial — full design for Native-First Interaction: 4 modes (preview / button keyboard / input box / attach), key forwarding mapping, send_raw_keys, ANSI capture, scroll lock, REQ-013 supersession | ALL | REQ-015 requirement.md v1; user explicitly requested deletion of OLD input row and a hybrid A+C interaction model |
