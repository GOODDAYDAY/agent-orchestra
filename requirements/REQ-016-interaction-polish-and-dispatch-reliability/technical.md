# REQ-016 Technical Design — Interaction Polish & Dispatch Reliability

> Status: Completed
> Requirement: requirement.md
> Created: 2026-04-08
> Updated: 2026-04-08

## 1. Technology Stack

| Area | Technology | Rationale |
|:---|:---|:---|
| Collapsible admin row | Textual CSS class toggle (`collapsed` → `display: none`) | Zero JS, pure CSS, reversible |
| Key forwarding | New helper `key_forwarding.tmux_args_for_key(event)` using `event.character` | Handles shift-modified punctuation that Textual reports via `event.character` not `event.key` |
| Concurrent start/stop | `asyncio.gather(return_exceptions=True)` | Standard library, no new deps; allows per-agent exceptions without cancelling the rest |
| Dispatch parser leniency | Two regex patterns (full + self-closing) tried in order | Minimum surface area change, backward compatible |
| Dispatch newline normalisation | `str.replace("\n", " ")` before send_keys | Simple, safe, only affects the dispatch_loop path |
| Diagnostic logging | stdlib `logging` with signature dedup | Existing logging infrastructure |
| Skill hints in workflows | New `Step.skill: Optional[str]` field + render update | Backward-compatible dataclass extension |
| Template version bump | `_TEMPLATE_VERSION = 6` + force-update logic (existing) | Same pattern as every prior template change |

## 2. Design Principles

- **Five narrow fixes, one REQ** — each F-item is small and localised; bundling them reduces commit noise and surface area
- **Fail loud, not silent** — F-04d adds logging so the next "dispatch doesn't work" report has diagnostic data from the first minute
- **Backward-compatible parsers** — the new self-closing dispatch form is additive; the old full form still works
- **Skill knowledge lives in data, not prose** — `Step.skill` is a structured field; the render helper interpolates it into orchestrator prompts and worker prompts reference it uniformly

## 3. Architecture Overview

Modules touched:

```
backend/workflows.py             ✏️  Step.skill field, 3 workflows updated, render updated
backend/session_manager.py       ✏️  send_keys cat fallback deleted
backend/supervisor.py            ✏️  start/stop/resume_group use asyncio.gather;
                                     dispatch_loop strips newlines + diagnostic log
backend/repository.py            ✏️  _TEMPLATE_VERSION 5→6; orchestrator + worker templates
                                     updated to reference /req-* skill invocation
backend/orchestrator.py          ✏️  parse_latest_dispatch accepts self-closing form
frontend/key_forwarding.py       ✏️  new tmux_args_for_key(event) helper
frontend/agent_pane.py           ✏️  admin row collapsible; InputBox uses tmux_args_for_key
tests/test_key_forwarding.py     ✏️  tmux_args_for_key tests + punctuation via character
tests/test_agent_pane.py         ✏️  admin toggle test + punctuation regression
tests/test_supervisor_concurrency.py  ✨ NEW — asyncio.gather timing proof
tests/test_orchestrator.py       ✏️  self-closing form tests
tests/test_dispatch_integration.py    ✏️  newline strip + parse failure log tests
tests/test_workflows.py          ✏️  skill field coverage
tests/test_repository.py         ✏️  template version + /req-* mention
requirements/index.md            ✏️  REQ-016 row added
```

No new files required beyond `tests/test_supervisor_concurrency.py`. No dependency changes.

## 4. Module Design

### 4.1 `frontend/key_forwarding.py` — new `tmux_args_for_key` helper

**Contract:**

```python
def tmux_args_for_key(event) -> Optional[list[str]]:
    """Resolve a Textual events.Key event into tmux send-keys argv.

    Prefers event.key for named special keys (Enter, Tab, arrows, ctrl+*)
    and event.character for printable input (letters, digits, punctuation,
    IME). Falls back to textual_to_tmux(event.key) for older Textual
    versions where event.character may be absent.
    """
```

**Algorithm:**

```python
def tmux_args_for_key(event) -> Optional[list[str]]:
    key_name: str = getattr(event, "key", "") or ""

    # 1. Named special keys and ctrl combinations — event.key is authoritative.
    if key_name in _SPECIAL or key_name.startswith("ctrl+"):
        return textual_to_tmux(key_name)

    # 2. Printable character input — prefer event.character because Textual
    # may report shift-modified punctuation via event.key="exclamation_mark"
    # while event.character holds the real "!".
    character = getattr(event, "character", None)
    if character is not None and character.isprintable() and character != "":
        return textual_to_tmux(character)

    # 3. Fallback — older Textual versions that only set event.key.
    if key_name:
        return textual_to_tmux(key_name)

    return None
```

`_SPECIAL` is the existing module-level dict. The function is exported alongside `textual_to_tmux` so call sites can import it directly.

**Unit tests** use stub event objects (`SimpleNamespace`) so no Textual app is needed.

### 4.2 `frontend/agent_pane.py` — collapsible admin row + new on_key path

**Layout change:**

The `pane-controls` Horizontal container is extended with a CSS class `collapsed`. The header gains a new toggle button:

```python
with Horizontal(classes="pane-header"):
    yield Label(...)  # name
    yield Label(...)  # status badge
    yield Label(...)  # role marker
    yield Button("⋯", id=f"btn-admin-toggle-{self.agent.id}", classes="admin-toggle", compact=True)
    yield Button("Enter", id="enter-agent", variant="primary", compact=True)
```

The Enter button is promoted from the admin row into the header so it stays accessible when the admin row is collapsed.

The admin row itself drops its Enter button (since it's now in the header):

```python
with Horizontal(classes="pane-controls collapsed"):
    yield Button("Pause", ...)
    yield Button("Resume", ...)
    yield Button("Edit", ...)
    yield Button("Restart", ...)
    yield Button("Delete", ...)
```

CSS additions:

```css
AgentPane .pane-controls.collapsed {
    display: none;
}
AgentPane Button.admin-toggle {
    width: auto;
    margin-left: 1;
}
```

**Button handler addition:**

```python
if bid == f"btn-admin-toggle-{aid}":
    controls = self.query_one(".pane-controls", Horizontal)
    if "collapsed" in controls.classes:
        controls.remove_class("collapsed")
    else:
        controls.add_class("collapsed")
    return
```

**InputBox.on_key — punctuation fix:**

```python
async def on_key(self, event: events.Key) -> None:
    from agent_management.frontend.key_forwarding import tmux_args_for_key
    spec = tmux_args_for_key(event)
    if spec is None:
        return
    # ... rest unchanged (double-esc exit, event.stop, post KeyForwarded)
```

**Local echo update:** the echo accumulator now uses `event.character` for the displayed text when available:

```python
display_char = event.character if event.character else event.key
if event.key == "enter":
    self._reset_echo()
elif event.key == "backspace":
    ...
elif display_char and len(display_char) == 1 and display_char.isprintable():
    self._echo += display_char
    self._render_echo()
```

### 4.3 `backend/supervisor.py` — concurrent start/stop/resume

**start_group:**

```python
async def start_group(self, group_id: str) -> None:
    logger.info("Starting group %s", group_id)
    await self._cancel_dispatch_loop()
    self._active_group_id = group_id
    self._last_dispatch_raw = None
    self._workflow_ended = False
    self._step_index = 0
    self._dev_tester_retries = 0
    self._stall_notified = False

    members = await self._repo.get_group_members(group_id)
    workers = [a for a in members if a.role != AgentRole.orchestrator]
    orch_agent = next((a for a in members if a.role == AgentRole.orchestrator), None)

    # REQ-016 F-03: start workers concurrently via asyncio.gather.
    results = await asyncio.gather(
        *[self._sm.start_agent_session(w, group_id, resume_session_id=None)
          for w in workers],
        return_exceptions=True,
    )
    for worker, result in zip(workers, results):
        if isinstance(result, Exception):
            logger.exception("Failed to start %s: %s", worker.name, result)
            await self._repo.update_agent_status(worker.id, AgentStatus.degraded)
            self._app.post_message(AgentStatusChanged(
                agent_id=worker.id, status=AgentStatus.degraded))
        else:
            self._app.post_message(AgentStatusChanged(
                agent_id=worker.id, status=AgentStatus.active))

    # Verify workers active before starting orchestrator (unchanged logic)
    not_active = []
    for w in workers:
        sess = await self._repo.get_session(w.id, group_id)
        if not sess or sess.status != AgentStatus.active:
            not_active.append(w.name)
    if not_active:
        logger.error("Refusing to start orchestrator — workers not active: %s", not_active)
        return

    if orch_agent is None:
        logger.warning("Group %s has no orchestrator agent", group_id)
        return

    try:
        await self._sm.start_agent_session(orch_agent, group_id, resume_session_id=None)
        self._app.post_message(AgentStatusChanged(
            agent_id=orch_agent.id, status=AgentStatus.active))
    except Exception:
        logger.exception("Failed to start orchestrator for group %s", group_id)
        return

    # Spawn the dispatch loop as before
    self._force_advance_request = asyncio.Event()
    self._abort_request = asyncio.Event()
    self._dispatch_task = asyncio.create_task(
        self._dispatch_loop(group_id, orch_agent),
        name=f"dispatch-{group_id[:8]}",
    )
```

**resume_group:** same pattern — workers parallel, orchestrator serial last.

**stop_group:**

```python
async def stop_group(self, group_id: str) -> None:
    logger.info("Stopping group %s", group_id)
    await self._cancel_dispatch_loop()
    sessions = await self._repo.get_sessions_for_group(group_id)

    results = await asyncio.gather(
        *[self._sm.stop_agent_session(s) for s in sessions],
        return_exceptions=True,
    )
    for session, result in zip(sessions, results):
        if isinstance(result, Exception):
            logger.exception("Error stopping session %s: %s", session.id, result)
        else:
            self._app.post_message(AgentStatusChanged(
                agent_id=session.agent_id, status=AgentStatus.stopped))

    if self._active_group_id == group_id:
        self._active_group_id = None
```

### 4.4 `backend/session_manager.py` — drop cat fallback

**Before:**

```python
async def send_keys(self, pane_id: str, text: str) -> None:
    safe_text = self._sanitize_payload(text)
    if len(safe_text) <= DIRECT_SEND_MAX_LEN:
        rc, _, err = await self._tmux("send-keys", "-t", pane_id, safe_text, "Enter")
        if rc != 0:
            logger.warning(...)
    else:
        tmp_path = ...
        tmp_path.write_text(safe_text, ...)
        ...
        cmd = f"cat {shlex.quote(str(tmp_path))}"
        rc, _, err = await self._tmux("send-keys", "-t", pane_id, cmd, "Enter")
        ...
```

**After:**

```python
async def send_keys(self, pane_id: str, text: str) -> None:
    """Send text to a tmux pane as a single keystroke stream + Enter.

    REQ-016 F-04b: the old cat-fallback path is deleted. It was designed for
    shell panes; inside a Claude CLI pane, 'cat /path' is interpreted as a
    literal chat message and the dispatched content never reached the agent.
    tmux send-keys accepts multi-KB argv tokens on all target OSes, so we can
    send any payload directly.
    """
    safe_text = self._sanitize_payload(text)
    if not safe_text:
        return
    rc, _, err = await self._tmux("send-keys", "-t", pane_id, safe_text, "Enter")
    if rc != 0:
        logger.warning("send-keys failed for pane %s: %s", pane_id, err)
```

The `_cleanup_temp` helper and the `shlex` import stay because they are still used by the orchestrator prompt tmp file (REQ-014 F-02) and session_manager.start_agent_session's shell command construction respectively.

### 4.5 `backend/supervisor.py` — newline normalisation + diagnostic log

Inside `_dispatch_loop`, at the point where we call `send_keys` with `dispatch.text`:

```python
# REQ-016 F-04c: strip embedded newlines so tmux doesn't submit early.
clean_text = dispatch.text.replace("\r\n", " ").replace("\n", " ")
await self._sm.send_keys(worker_session.tmux_pane_id, clean_text)
```

New field on Supervisor:

```python
self._last_parse_warning: Optional[str] = None
```

In `dispatch_loop`, after `parse_latest_dispatch(pane_text)` returns `None`:

```python
if dispatch is None:
    # REQ-016 F-04d: diagnostic log when the pane contains <<DISPATCH but
    # the parser couldn't extract anything. Dedup by tail signature so the
    # log doesn't spam every 500 ms.
    if "<<DISPATCH" in pane_text:
        tail = pane_text[-200:].replace("\n", "\\n")
        if tail != self._last_parse_warning:
            logger.warning(
                "dispatch_loop: <<DISPATCH seen in orch pane but parse failed "
                "group=%s tail=%r", group_id, tail,
            )
            self._last_parse_warning = tail
    continue
```

### 4.6 `backend/orchestrator.py` — lenient dispatch parser

**Two regex constants:**

```python
# Full form — with closing tag and optional body
_DISPATCH_RE_FULL = re.compile(
    r'<<DISPATCH\s+role="(?P<role>[a-z_]+)"\s+text="(?P<text>(?:[^"\\]|\\.)*)"\s*>>'
    r'(?P<body>.*?)'
    r'<</DISPATCH>>',
    re.DOTALL,
)

# Self-closing form — optional trailing slash, no body, no closing tag.
# This is the LLM-friendly shape; the orchestrator template recommends it.
_DISPATCH_RE_SELF_CLOSING = re.compile(
    r'<<DISPATCH\s+role="(?P<role>[a-z_]+)"\s+text="(?P<text>(?:[^"\\]|\\.)*)"\s*/?\s*>>',
)
```

**`parse_latest_dispatch` algorithm:**

1. Scan `orchestrator_pane_text` with `_DISPATCH_RE_FULL` (collect all matches after `after_offset`). Keep the last one if any.
2. Scan with `_DISPATCH_RE_SELF_CLOSING` (collect all matches after `after_offset`). Keep the last one if any.
3. If **both** forms matched, pick whichever has the larger `end_offset` (whichever was emitted last in the pane text).
4. Return a `Dispatch` built from the chosen match, or `None` if neither matched.

Crucially, the self-closing regex is a prefix of the full regex, so both will match a full-form dispatch. Step 3's "larger end_offset" rule ensures we still consume the full form (which extends further) rather than matching only the self-closing prefix.

**Implementation:**

```python
def parse_latest_dispatch(
    orchestrator_pane_text: str,
    after_offset: int = 0,
) -> Optional[Dispatch]:
    if after_offset >= len(orchestrator_pane_text):
        return None

    best: Optional[Dispatch] = None

    # Full form first
    for m in _DISPATCH_RE_FULL.finditer(orchestrator_pane_text):
        if m.end() <= after_offset:
            continue
        best = Dispatch(
            role=m.group("role").lower(),
            text=_unescape_text(m.group("text")),
            raw=m.group(0),
            end_offset=m.end(),
        )

    # Then self-closing, prefer if it ends later
    for m in _DISPATCH_RE_SELF_CLOSING.finditer(orchestrator_pane_text):
        if m.end() <= after_offset:
            continue
        candidate = Dispatch(
            role=m.group("role").lower(),
            text=_unescape_text(m.group("text")),
            raw=m.group(0),
            end_offset=m.end(),
        )
        if best is None or candidate.end_offset > best.end_offset:
            best = candidate

    return best
```

**Test additions** in `tests/test_orchestrator.py`:

- Self-closing form without slash: `<<DISPATCH role="dev" text="x">>`
- Self-closing form with trailing slash: `<<DISPATCH role="dev" text="x"/>>`
- Self-closing with extra whitespace: `<<DISPATCH  role="dev"  text="x"  />>`
- Mixed: self-closing then full form in the same text — returns the one with later end offset
- Full form still works (backward compat)

### 4.7 `backend/workflows.py` — `Step.skill` field + render update

**Step dataclass:**

```python
@dataclass(frozen=True)
class Step:
    role: AgentRole
    description: str
    skill: Optional[str] = None                # REQ-016 F-05
    on_failure_marker: Optional[str] = None
    failure_loop_to: Optional[int] = None
    max_retries: int = 0
```

**Built-in workflow updates:**

```python
STANDARD = Workflow(
    id="standard",
    display_name="Standard (PM → TD → Dev → Tester → User)",
    description="Full requirement-to-acceptance pipeline...",
    steps=(
        Step(role=AgentRole.product_manager,
             description="Produce a complete requirement specification using /req-1-analyze.",
             skill="req-1-analyze"),
        Step(role=AgentRole.tech_director,
             description="Review the spec and produce a technical design using /req-2-tech.",
             skill="req-2-tech"),
        Step(role=AgentRole.developer,
             description=(
                 "Implement the technical design. You must run the full "
                 "/req-3-code → /req-4-security → /req-5-cleanup → /req-6-review "
                 "→ /req-7-verify pipeline before declaring the step complete."
             ),
             skill="req-3-code"),
        Step(role=AgentRole.tester,
             description="Run the test suite using /req-7-verify and report results.",
             skill="req-7-verify",
             on_failure_marker="<<TESTS_FAILED>>",
             failure_loop_to=2,
             max_retries=3),
        Step(role=AgentRole.user,
             description="Acceptance review by the human (or human stand-in) user."),
    ),
)

PROTOTYPE = Workflow(
    id="prototype",
    display_name="Prototype (Dev → User)",
    description="Two-step workflow for quick experiments.",
    steps=(
        Step(role=AgentRole.developer,
             description="Implement the prototype using /req-3-code.",
             skill="req-3-code"),
        Step(role=AgentRole.user,
             description="Acceptance review of the prototype."),
    ),
)

RESEARCH = Workflow(
    id="research",
    display_name="Research (PM → TD → User)",
    description="Design-only workflow with no coding phase.",
    steps=(
        Step(role=AgentRole.product_manager,
             description="Frame the research question using /req-1-analyze.",
             skill="req-1-analyze"),
        Step(role=AgentRole.tech_director,
             description="Investigate and produce technical findings using /req-2-tech.",
             skill="req-2-tech"),
        Step(role=AgentRole.user,
             description="Acceptance review of the findings."),
    ),
)
```

**`render_for_orchestrator` update:**

```python
def render_for_orchestrator(
    workflow: Workflow,
    roster: list[tuple[AgentRole, str, str]],
) -> str:
    lines: list[str] = [f"Workflow: {workflow.display_name}", ""]
    name_by_role: dict[AgentRole, str] = {role: name for role, name, _ in roster}
    for idx, step in enumerate(workflow.steps, start=1):
        actor = name_by_role.get(step.role, f"<missing {step.role.value}>")
        skill_note = (
            f"  ⚡ must invoke /{step.skill}" if step.skill else "  (no skill — human review)"
        )
        line = f"  {idx}. {step.role.value}  ({actor})  —  {step.description}\n  {skill_note}"
        if step.on_failure_marker and step.failure_loop_to is not None:
            target_idx = step.failure_loop_to + 1
            line += (
                f"\n     If output contains {step.on_failure_marker}, loop back "
                f"to step {target_idx} (max {step.max_retries} retries)."
            )
        lines.append(line)
    return "\n".join(lines)
```

### 4.8 `backend/repository.py` — template version bump + /req-* instructions

**Version bump:**

```python
_TEMPLATE_VERSION = 6
```

**Orchestrator template — new `## 技能调用规则` section:**

Insert after the existing `## 调度协议` section:

```text
## 技能调用规则

工作流里的每一步都可能标注了一个必须调用的 /req-* 技能（比如 /req-1-analyze、
/req-2-tech、/req-3-code、/req-7-verify）。当你 dispatch 某个角色时：

1. 如果该步骤标注了技能（⚡ must invoke /req-X），你的 dispatch text 必须
   明确告诉该 worker 去调用这个技能。推荐格式：

     <<DISPATCH role="developer" text="请调用 /req-3-code 技能，目标是：
     实现本次需求（见上一个 [WORKER_RESULT] 里的技术方案）。完成后在最后
     一行输出 <<TASK_DONE>>。">>

2. 如果该步骤没有技能（human review step），正常下达指令即可。

3. 不要自己捏造技能名 —— 只用工作流定义里出现的 /req-* 名字。
```

**Worker template additions (applied to PM / TD / Developer / Tester; User and Custom unchanged):**

A new section appended at the end of each existing template:

```text
## 技能调用规则
当 Orchestrator 的 prompt 里提到某个 /req-* 技能（比如 /req-1-analyze），你
必须在自己的终端里调用该技能，方法是让它成为你的 response 的首个动作。执行
完毕再产出最终结果。
```

For the Developer specifically, the note also reminds to chain `/req-3-code → /req-4-security → /req-5-cleanup → /req-6-review → /req-7-verify`.

**Self-closing dispatch example in orchestrator template:** the existing examples are updated to show self-closing form as the recommended shape, while still mentioning that closing tags are accepted:

```text
  <<DISPATCH role="developer" text="...">>

  就这一行。不需要关闭标签；平台同时也接受 <</DISPATCH>> 形式的关闭标签。
```

## 5. Data Model

No schema changes. `Step.skill` lives in code only (in-memory dataclass).

## 6. API Design

No external API changes. Internal additions:

| API | Module | Purpose |
|:---|:---|:---|
| `tmux_args_for_key(event) -> Optional[list[str]]` | `frontend/key_forwarding.py` | Resolve Textual Key event to tmux argv using character-priority logic |
| `Step.skill: Optional[str]` | `backend/workflows.py` | Per-step skill hint |

## 7. Key Flows

### 7.1 Punctuation forwarded via character

1. User focuses the InputBox and presses Shift+1 (types `!`)
2. Textual emits `events.Key(key="exclamation_mark", character="!")` (or similar — varies by version)
3. `InputBox.on_key` calls `tmux_args_for_key(event)`
4. `key="exclamation_mark"` is not in `_SPECIAL` and not a ctrl combo
5. `character="!"` is a single printable char → returns `textual_to_tmux("!")` → `["!"]`
6. InputBox posts `KeyForwarded(agent_id, ["!"])`
7. App's handler calls `send_raw_keys(pane_id, "!")`
8. Tmux receives `tmux send-keys -t {pane} "!"` → agent sees `!`

### 7.2 Concurrent group start

1. Operator clicks Start Group (6 agents)
2. Supervisor gathers 5 worker start coroutines
3. All 5 tmux `new-window` + `send-keys claude ...` calls fire roughly simultaneously
4. Each worker runs its own readiness poll; the first to respond updates its status
5. `asyncio.gather` returns when all 5 have finished (or raised)
6. Supervisor verifies all active, starts the orchestrator serially (because orchestrator needs the roster with live pane IDs)
7. Dispatch loop task spawned

### 7.3 Dispatch via self-closing form

1. Orchestrator emits `<<DISPATCH role="developer" text="implement feature X">>`
2. Supervisor polls orch pane, sees the text
3. `parse_latest_dispatch` tries full form first (no match — no closing tag)
4. Tries self-closing form → matches → returns a Dispatch
5. Dispatch proceeds normally

### 7.4 Diagnostic log on parse failure

1. Orchestrator emits malformed dispatch: `<<DISPATCH role="devX" text="unterminated`
2. Parser runs, both patterns fail, returns None
3. dispatch_loop: "<<DISPATCH" is in pane_text → log warning with last 200 chars as tail
4. Operator opens `.agent_management/platform.log` → sees exactly what the orchestrator emitted

## 8. Shared Modules & Reuse Strategy

| Shared | Used by | Notes |
|:---|:---|:---|
| `tmux_args_for_key` | InputBox (and future broadcast features) | Pure function, fully tested with stub events |
| `asyncio.gather(return_exceptions=True)` | supervisor.start/stop/resume_group | Standard library; no new wrapper |
| `_DISPATCH_RE_SELF_CLOSING` | orchestrator.parse_latest_dispatch | Lives next to the existing full-form regex |
| `Step.skill` field | workflows.render_for_orchestrator, orchestrator template | Data-driven; zero hardcoded /req-* names in orchestrator template except in examples |

## 9. Risks & Notes

| Risk | Mitigation |
|:---|:---|
| Textual's event.character naming may differ across versions | Fall back to event.key; tests use `SimpleNamespace` stubs covering both shapes |
| Concurrent start causes tmux contention on `new-session` | `new-session` is idempotent and the first call wins; subsequent calls see an existing session. `new-window` is always safe in parallel. |
| One worker failing in parallel start may confuse the subsequent "all active?" check | `return_exceptions=True` + the post-gather verification loop handles this explicitly |
| Self-closing regex matches too greedily on full form | Step 3 in the parse algorithm picks whichever has the later `end_offset`, so full-form dispatches consume past the closing tag |
| Dropping the cat fallback loses multi-KB send capability | tmux send-keys accepts argv tokens up to ARG_MAX (256 KB to 2 MB on Linux, ~32 KB on Windows); the `_sanitize_payload` 50 KB cap fits comfortably |
| Template bump overwrites customised prompts | Same known limitation as every previous bump; users must re-apply customisations |
| Orchestrator LLM may not follow the skill-invocation rule | Acceptable — the rule is in the prompt but enforcement is LLM-side. The human operator can see the dispatch text in the orchestrator pane and intervene if the LLM forgets |
| `/req-*` slash commands may not be recognised by Claude CLI as tools | Both paths work: (a) Claude CLI intercepts `/req-x` at line start, (b) Claude as an LLM can invoke `/req-x` as a tool call when instructed. The worker prompt is written to trigger path (b). |

## 10. Test Strategy

New/updated tests:

| Test file | Scope | Tests |
|:---|:---|:---|
| `tests/test_key_forwarding.py` | `tmux_args_for_key` | ~12 tests: punctuation via character, special keys via key, ctrl combos, fallback when character missing, None on garbage |
| `tests/test_agent_pane.py` | Admin row toggle | ~4 tests: initial state (collapsed), click toggles visibility, Enter button in header, click again hides |
| `tests/test_agent_pane.py` | Punctuation regression | ~3 tests: pilot types `!`, `@`, `{` into InputBox and asserts forwarding |
| `tests/test_supervisor_concurrency.py` (new) | asyncio.gather timing | ~4 tests: start_group parallelism (instrumented fake SM), stop_group parallelism, resume_group, one-failing-worker isolation |
| `tests/test_orchestrator.py` | Self-closing parser | ~6 tests: self-closing without slash, with slash, with whitespace, backward-compat full form still works, mixed forms picks later, after_offset respected |
| `tests/test_dispatch_integration.py` | Newline strip + diagnostic log | ~2 tests: dispatch text with `\n` is normalised before send_keys; malformed `<<DISPATCH` triggers caplog warning |
| `tests/test_workflows.py` | `Step.skill` coverage | ~5 tests: every non-user step has a skill; render output contains `/req-*`; None skill renders "no skill" note |
| `tests/test_repository.py` | Template version bump + skill references | ~3 tests: _TEMPLATE_VERSION == 6; orchestrator template contains `/req-` and `技能调用规则`; worker templates mention `/req-*` |

## 11. Change Log

| Version | Date | Changes | Affected Scope | Reason |
|:---|:---|:---|:---|:---|
| v1 | 2026-04-08 | Initial — F-01 collapsible admin controls, F-02 tmux_args_for_key helper using event.character, F-03 asyncio.gather for start/stop/resume_group, F-04a parse_latest_dispatch accepts self-closing form, F-04b cat fallback deleted from send_keys, F-04c dispatch text newline normalisation in dispatch_loop, F-04d diagnostic log on parse failure, F-05 Step.skill field + orchestrator/worker template updates + template version bump to 6 | ALL | Five concrete issues reported after REQ-015 live testing |
