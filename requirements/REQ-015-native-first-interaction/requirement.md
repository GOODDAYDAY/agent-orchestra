# REQ-015 Native-First Interaction

> Status: Completed
> Created: 2026-04-08
> Updated: 2026-04-08
> Supersedes: REQ-013 (terminal-attach-interaction) — all REQ-013 features absorbed.

## 1. Background

### 1.1 The previous interaction model is broken

REQ-001 introduced a per-pane `Input` widget with a `Send` button: the user types a line, hits Send, and the AgentPane forwards the text to the agent's tmux pane via a single `send_keys` call. This is a bad model:

- **Interactive prompts don't work.** When Claude CLI shows `[Y/n]` or a selection menu, the user can't respond — the only way to type is to compose a full line and hit Send, but the agent has already moved past the prompt by the time the user finishes typing.
- **No real-time feedback.** Tab completion, arrow-key history, in-line edit — all impossible.
- **No control keys.** Ctrl+C, Ctrl+D, Esc — none reachable.
- **The text input box's existence is misleading.** It looks like a terminal, behaves like a chat-room input, satisfies neither.

REQ-013 already analysed this and laid out a plan to make `Enter`/`Attach` the primary path, fix the output scrollbar, and add quick-action buttons — but explicitly deferred deleting the input box "to a future REQ at the user's explicit request". REQ-015 is that explicit request, and it goes one step further by also adding a **dedicated input box that operates in pure key-forwarding mode** so users can type in real time without leaving the TUI.

### 1.2 Why pure native interaction inside Textual is impossible

Textual has no terminal-emulator widget. Embedding a real PTY-rendered terminal would require either a third-party `textual-terminal` package (immature, depends on `pyte`, would force the agent off tmux entirely and break the orchestrator dispatch loop) or building one from scratch (months of work). Both options are off the table for this REQ.

### 1.3 What "native-first" actually means in v15

REQ-015 ships **four interaction modes**, each appropriate for its use case, with no half-measures:

1. **Read-only preview (look)** — RichLog rendering ANSI colours, scroll-lock fix.
2. **Button keyboard (one-tap)** — eight pre-configured quick-action buttons covering `Continue`, `Y`, `N`, `Esc`, `Ctrl+C`, `↑`, `↓`, `Ctrl+D`.
3. **Dedicated input box (real-time typing)** — focusable widget that, when focused, captures every keystroke and immediately forwards it to the agent's tmux pane via `send_keys`. Tab, arrows, Enter, Ctrl+chars all work. This is "Option C" pure key forwarding scoped to a clean affordance.
4. **Full Attach (escape hatch)** — when the read-only preview has focus, pressing **Enter** triggers the REQ-011 grouped-tmux-attach path. Used for anything the first three modes can't handle (long compose sessions, copy-paste with mouse selection, ANSI cursor games, etc.).

The OLD `Input` + `Send` button widgets are deleted. The new dedicated input box is a different beast (focus catcher, not a text editor).

## 2. Target Users & Scenarios

| ID | Scenario | Mode used |
|:---|:---|:---|
| S-01 | Operator wants to glance at what the Developer agent is doing | Read-only preview |
| S-02 | Claude CLI shows `[Y/n]`, user wants to confirm | Button keyboard (Y) |
| S-03 | Claude CLI shows a selection menu (arrow keys) | Button keyboard (↑ / ↓) then `Y` or input-box Enter |
| S-04 | User wants to send `git status` to a worker pane and watch the result | Dedicated input box (real-time typing) |
| S-05 | User wants to do extensive interactive debugging (paste large input, scroll output, view ANSI cursor app) | Full Attach via Enter on focused preview |
| S-06 | User wants to scroll back and read agent output history | Read-only preview scroll-lock fix |
| S-07 | User wants to interrupt a runaway worker | Button keyboard (Ctrl+C) |

## 3. Functional Requirements

### F-01 Delete the OLD Input + Send affordance

- Main flow: remove the per-pane `Input` widget (`#inp-send-{agent_id}`) and `Send` button (`#btn-send-{agent_id}`) from `AgentPane.compose()`. Remove the `SendRequested` Textual message and the `on_input_submitted` / button-click handlers that produced it. Remove the `on_agent_pane_send_requested` handler in `app.py`.
- Rationale: the old input box never satisfied any real interaction need; it confused users into thinking the TUI was a terminal it isn't.
- Edge cases: existing tests that exercise `SendRequested` must be updated or deleted.

### F-02 Read-only preview with ANSI rendering and scroll lock (absorbs REQ-013 F-03 / F-04)

- Main flow: each AgentPane's `RichLog` widget displays the live `tmux capture-pane` output. The capture call uses the `-e` flag so ANSI escape codes are preserved, and the AgentPane converts the captured bytes via `rich.text.Text.from_ansi()` before writing them into the log so colours and bold render correctly.
- Scroll-lock behaviour: the RichLog is reconfigured with `auto_scroll=False`. Each refresh checks whether the user is currently scrolled to the bottom; if so, the log scrolls to the bottom after the new content is written; if not, the new content is appended silently and the viewport stays put.
- "Jump to latest" affordance: when the user is not at the bottom, a small button or label `↓ jump to latest` appears at the bottom edge of the preview area. Clicking it jumps to the bottom and re-enables auto-follow.
- End key: pressing `End` while the preview is focused jumps to the bottom and re-enables follow.
- Error handling: if `capture-pane` fails (pane vanished), the preview shows the last successful capture plus an `(agent stopped)` footer line and stops polling.
- Edge cases: the capture-pane output may exceed RichLog buffer; existing buffer cap of `OUTPUT_BUFFER_LINES = 500` (new constant) trims oldest lines silently.

### F-03 Button keyboard — eight quick actions

- Main flow: replace the existing `Pause / Resume / Edit / Restart / Delete / Enter` controls row with **two rows**:
  - **Row 1 (admin controls, unchanged)**: `Pause`, `Resume`, `Edit`, `Restart`, `Delete`, `Enter`.
  - **Row 2 (quick keyboard, new)**: `Continue`, `Y`, `N`, `Esc`, `^C`, `↑`, `↓`, `^D`.
- Each button click invokes a single `tmux send-keys -t {pane_id} {key_spec}` (no trailing Enter unless the spec already includes one):
  | Button | Tmux key spec | Description |
  |:---|:---|:---|
  | Continue | `continue Enter` | Sends literal `continue\n` (Claude CLI's "keep going" command) |
  | Y | `y Enter` | Single 'y' followed by Enter — confirms `[Y/n]` prompts |
  | N | `n Enter` | Single 'n' followed by Enter |
  | Esc | `Escape` | Sends a real Escape keystroke (cancels selection menus, closes Claude CLI dialogs) |
  | ^C | `C-c` | Sends Ctrl+C (interrupt) |
  | ↑ | `Up` | Up-arrow keystroke (navigates Claude CLI selection menus, history) |
  | ↓ | `Down` | Down-arrow keystroke |
  | ^D | `C-d` | Ctrl+D (EOF / end of input) |
- Buttons are enabled only when the agent has an `active` session AND no attach is in progress.
- Error handling: if `send-keys` fails (pane vanished), show toast `"Agent pane gone — restart the agent"`.
- Edge cases: rapid double-click on the same button sends two keystrokes; this is intentional (matching tmux real behaviour).

### F-04 Dedicated input box with pure key forwarding

- Main flow: each AgentPane gains a new focusable widget below the button keyboard, displayed as a single-line affordance with the placeholder text `⌨ click here to type to agent (Esc to leave)`. When focused, the widget captures every Textual `Key` event and forwards it to the agent's tmux pane via the new `SessionManager.send_raw_keys` method.
- Key forwarding: each Textual key is mapped to its tmux send-keys notation (see F-05). The mapping covers all printable ASCII, all control keys (Ctrl+letter), arrow keys, function keys, Tab, Enter, Backspace, Delete, Home, End, Page Up/Down, Esc, and unicode characters typed via the user's IME.
- Echo: while the focus catcher is active, typed printable characters are echoed locally into the widget label so the user gets instant visual feedback (no waiting on capture-pane refresh). The echo is purely cosmetic and reset on Enter.
- Leaving the input box: pressing **Esc** twice (or clicking outside the widget) returns focus to the AgentPane container. Single Esc is forwarded to the agent (Claude CLI uses Esc to cancel selection menus). Pressing Tab inside the input box forwards Tab to the agent (does not move TUI focus).
- Special keys that DO leave forwarding mode: none in v1. Esc is forwarded; the user must double-press Esc or click outside.
- Error handling: if `send_raw_keys` fails, the input box flashes red briefly and shows toast `"Agent pane gone"`.
- Edge cases: when multiple AgentPanes are visible, only one input box can have focus at a time. Switching focus to another pane's input box is allowed and immediate.

### F-05 Textual key → tmux key spec mapping

- Main flow: a new pure-function module `frontend/key_forwarding.py` exposes `textual_to_tmux(event_key: str) -> Optional[list[str]]` that maps Textual `events.Key.key` strings to a list of tmux send-keys arguments.
- Mapping table (canonical):

  | Textual key | Tmux argv |
  |:---|:---|
  | `enter` | `["Enter"]` |
  | `tab` | `["Tab"]` |
  | `backspace` | `["BSpace"]` |
  | `delete` | `["DC"]` |
  | `escape` | `["Escape"]` |
  | `up` / `down` / `left` / `right` | `["Up"]` etc. |
  | `home` / `end` | `["Home"]` / `["End"]` |
  | `pageup` / `pagedown` | `["PPage"]` / `["NPage"]` |
  | `f1`..`f12` | `["F1"]`..`["F12"]` |
  | `space` | `["Space"]` |
  | `ctrl+a`..`ctrl+z` | `["C-a"]`..`["C-z"]` |
  | `ctrl+space` | `["C-Space"]` |
  | `ctrl+]` | `["C-]"]` |
  | Single printable char (e.g. `a`, `1`, `"`) | `[char]` |
  | Multi-char unicode | `[char]` (tmux send-keys accepts UTF-8) |
  | Unsupported | `None` (forwarded as no-op) |

- Validation: the mapping function must return `None` for unrecognised keys rather than raising — the input box treats `None` as "drop the keystroke silently".
- Edge cases: `event.key == ""` returns `None`. Modifier-only events (just `ctrl` with no letter) are ignored.

### F-06 Enter-on-focused-preview triggers Attach

- Main flow: when the AgentPane's RichLog (read-only preview) has focus and the user presses **Enter**, the AgentPane posts `AttachRequested(agent_id)` — exactly the same event the existing Enter button posts. The downstream attach flow is unchanged.
- The Enter button on the admin controls row remains as a mouse-friendly affordance.
- Distinguishing focus contexts: if the focused widget is the input box (F-04), Enter is forwarded to the agent — never attaches. The AgentPane's container-level `on_key` handler only fires when the input box's own handler did not call `event.stop()`.
- Edge cases: if the agent is not active (no live pane), the existing F-06 toasts from REQ-012 v2 fire instead.

### F-07 SessionManager.send_raw_keys

- Main flow: a new async method `SessionManager.send_raw_keys(pane_id: str, *key_args: str)` invokes `tmux send-keys -t {pane_id} <key_args...>` with no trailing Enter and no payload sanitisation (key specs are already constrained by the mapping function).
- Distinct from `send_keys`: the existing method (which appends Enter and sanitises text) is preserved for the orchestrator dispatch path; `send_raw_keys` is the new low-level escape hatch used by the input box and the button keyboard.
- Error handling: returns the tmux command's `(rc, stdout, stderr)` tuple so callers can react to failures (e.g. show a toast).
- Edge cases: empty `key_args` is a no-op; multiple key args are passed as separate arguments to `tmux send-keys`.

### F-08 SessionManager.capture_pane_full ANSI option

- Main flow: extend the existing `capture_pane_full` method to accept an `ansi: bool = False` keyword argument. When `True`, the underlying tmux call is `tmux capture-pane -p -e -S -<N> -t {pane_id}`; when `False` it stays as before. The agent_pane refresh uses `ansi=True`.
- Rationale: `-e` makes tmux emit ANSI escape sequences for colour and styling. Without it, the captured text is plain. The supervisor's dispatch_loop continues to call without `ansi=True` to keep parser input simple.
- Edge cases: ANSI captures may contain bytes that confuse downstream string operations; the supervisor's marker detector works on the plain capture, not the ANSI one.

### F-09 REQ-013 supersession

- Main flow: REQ-013's status in `requirements/index.md` changes from `Requirement Finalized` to `Superseded by REQ-015`. The REQ-013 directory and its requirement.md are preserved verbatim as historical analysis. A note is added at the top of REQ-013's requirement.md indicating supersession with a link reference.
- Rationale: REQ-013 has never been implemented; its functional requirements (F-01 Enter Attach, F-02 state indicator, F-03 output preview, F-04 scroll fix, F-05 quick actions) are wholly absorbed into REQ-015 with one addition (the dedicated input box) and one deletion (the OLD input row).

## 4. Non-functional Requirements

- All 189 existing tests must continue to pass.
- New tests added: at least 30 covering the key forwarding mapping, the new SessionManager methods, and the AgentPane focus state machine.
- No new runtime dependencies (no `textual-terminal`, no `pyte`).
- The button keyboard's button labels must use plain ASCII / UTF-8 symbols already in the project's codebase (no new emoji fonts required).
- Configuration constants in `shared/config.py`:
  | Constant | Default | Purpose |
  |:---|:---|:---|
  | `OUTPUT_POLL_INTERVAL_MS` | `500` | Read-only preview refresh rate |
  | `OUTPUT_BUFFER_LINES` | `500` | RichLog ring buffer cap |

## 5. Out of Scope

- True embedded terminal widget (Textual + pyte) — not happening in this REQ.
- Multi-line edit / paste in the input box — pure key forwarding only; users with multi-line needs use Attach.
- Input history (Up/Down arrow recall in the input box) — these arrows forward to the agent, not the input box.
- Custom button keyboard configuration UI — the eight buttons are hardcoded.
- Broadcast (send a key to multiple panes at once) — future REQ.
- Mouse selection / copy from the read-only preview — Textual RichLog already supports it; nothing new to do.
- Removing the existing admin controls row (Pause/Resume/Edit/Restart/Delete) — those stay.

## 6. Acceptance Criteria

| ID | Feature | Condition | Expected Result |
|:---|:---|:---|:---|
| AC-01 | F-01 | Open the TUI; create a group; observe an AgentPane | No `Input` or `Send` widgets are present below the controls row |
| AC-02 | F-01 | grep `src/agent_management/frontend/agent_pane.py` for `Input` | Zero matches inside AgentPane.compose; Input may still be referenced by dialogs |
| AC-03 | F-02 | Worker pane emits ANSI-coloured text | Preview shows text with the corresponding colours rendered (not literal escape codes) |
| AC-04 | F-02 | User scrolls preview up; new content arrives | Viewport stays at the scrolled position; `↓ jump to latest` indicator becomes visible |
| AC-05 | F-02 | User clicks `↓ jump to latest` | Viewport jumps to bottom; auto-follow re-enabled |
| AC-06 | F-02 | User is at bottom; new content arrives | Viewport auto-scrolls; no indicator |
| AC-07 | F-03 | User clicks the `Y` button | Tmux pane receives a single 'y' followed by Enter |
| AC-08 | F-03 | User clicks the `Esc` button | Tmux pane receives a real Escape keystroke (not the literal text "Esc") |
| AC-09 | F-03 | User clicks the `^C` button | Tmux pane receives Ctrl+C |
| AC-10 | F-03 | User clicks `↑` then `↓` | Tmux pane receives Up then Down arrow keystrokes |
| AC-11 | F-04 | User clicks the input box; types `git status` | Each character is forwarded to the agent pane immediately (one send_raw_keys call per character) |
| AC-12 | F-04 | User presses Enter inside the input box | Enter is forwarded to the agent; the input box echo clears |
| AC-13 | F-04 | User presses Tab inside the input box | Tab is forwarded; TUI focus does NOT move to next widget |
| AC-14 | F-04 | User presses Esc once inside the input box | Esc is forwarded (Claude CLI menu dismiss) |
| AC-15 | F-04 | User clicks outside the input box | Focus leaves; key forwarding deactivates |
| AC-16 | F-05 | textual_to_tmux("ctrl+c") | Returns `["C-c"]` |
| AC-17 | F-05 | textual_to_tmux("up") | Returns `["Up"]` |
| AC-18 | F-05 | textual_to_tmux("a") | Returns `["a"]` |
| AC-19 | F-05 | textual_to_tmux("unknown_key_xyz") | Returns `None` (no exception) |
| AC-20 | F-06 | User clicks the read-only preview; presses Enter | AttachRequested is posted (same path as the Enter button) |
| AC-21 | F-06 | User clicks the input box; presses Enter | Enter is forwarded to the agent — no attach is triggered |
| AC-22 | F-07 | send_raw_keys is called with `("C-c",)` | tmux send-keys is invoked with arguments `["send-keys", "-t", pane_id, "C-c"]` and no trailing Enter |
| AC-23 | F-08 | capture_pane_full(pane_id, ansi=True) | tmux command includes the `-e` flag |
| AC-24 | F-09 | requirements/index.md | REQ-013 status reads "Superseded by REQ-015"; REQ-015 status reads "Completed" |
| AC-25 | NFR | Run pytest | All 189 existing tests + new tests pass; total ≥ 220 |

## 7. Change Log

| Version | Date | Changes | Affected Scope | Reason |
|:---|:---|:---|:---|:---|
| v1 | 2026-04-08 | Initial version — delete OLD input row, add ANSI-rendering read-only preview with scroll lock, 8-button quick keyboard, dedicated input box with pure key forwarding via new send_raw_keys method, Enter-on-focused-preview attaches via REQ-011 path, REQ-013 superseded | ALL | User explicitly requested deletion of the old input box and asked for native interaction; combined Option A (button keyboard + attach) and Option C (key forwarding) per user direction |
