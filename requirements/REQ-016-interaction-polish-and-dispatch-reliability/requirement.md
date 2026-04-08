# REQ-016 Interaction Polish & Dispatch Reliability

> Status: Completed
> Created: 2026-04-08
> Updated: 2026-04-08

## 1. Background

After REQ-012 v2 (orchestrator pivot) and REQ-015 (native-first interaction) shipped, live testing revealed **five concrete problems**. REQ-016 fixes all of them in one pass.

### 1.1 Problem summary

| # | Symptom | Root cause |
|:---|:---|:---|
| 1 | The admin controls row (Pause / Resume / Edit / Restart / Delete) always consumes a row of vertical space per AgentPane, shrinking the read-only preview. | REQ-015 kept the admin row always visible. For 6 panes that's 6 lines of screen real estate sacrificed to buttons that are rarely clicked. |
| 2 | Typing punctuation (`!@#$%^&*(-_=+[]{};:'",.<>/?\|~`) into the dedicated input box has no effect — nothing gets forwarded to the agent. | REQ-015's InputBox calls `textual_to_tmux(event.key)`. On Textual for shift-modified punctuation keys `event.key` can be a named identifier (e.g. `"exclamation_mark"`, or empty, or version-dependent) while `event.character` holds the actual `!`. The key-forwarding function doesn't recognise the name and drops the keystroke silently. |
| 3 | `supervisor.start_group` / `stop_group` / `resume_group` start agents **one by one** in a `for` loop. With 6 agents and a 30-second readiness timeout each, worst-case group startup takes ~3 minutes even though the agents are independent and their tmux pane creations are trivially parallelisable. | The REQ-012 v2 implementation used sequential awaits for simplicity and never revisited it. |
| 4 | The orchestrator emits a dispatch block in its pane, but the target worker never receives the prompt. | Multiple cooperating root causes: (a) the parser only accepts `<<DISPATCH …>>…<</DISPATCH>>` with the closing tag, but an LLM often forgets or mutates the closing tag; (b) `SessionManager.send_keys` has a "long-payload fallback" that writes the text to a tmp file and sends `cat /path` to the pane — which works in a shell pane but is nonsense inside a Claude CLI pane (Claude receives the literal text `cat /path` as a chat message); (c) `send_keys` with a text containing embedded newlines causes tmux to interpret each `\n` as Enter, submitting only the first line; (d) when parsing silently fails, the dispatch_loop has no log output so the user has no diagnostic trail. |
| 5 | The orchestrator does not remind the worker agents to invoke the `/req-*` sub-skills (`/req-1-analyze`, `/req-2-tech`, `/req-3-code`, etc.) when dispatching work. The intended pipeline is "PM runs `/req-1-analyze`, Tech Director runs `/req-2-tech`, Developer runs the full `/req-3-code` → `/req-7-verify` chain", but the current orchestrator prompt has no knowledge of skill names and the workflow definitions carry only free-form text descriptions. | The REQ-012 v2 workflow Step dataclass has a `description` field but no `skill` field. The orchestrator template doesn't mention `/req-*` at all, so the LLM has no reason to include skill invocations in its dispatch text. Worker templates also don't instruct agents to invoke `/req-*` skills when mentioned. |

### 1.2 Why fix these together

All five are small individually and they all touch the same code paths (AgentPane layout, key forwarding, supervisor dispatch loop, role templates, workflow definitions). Bundling them in one REQ keeps the commit history coherent and the regression surface small.

## 2. Target Users & Scenarios

- **S-01 Small terminal**: operator is running the TUI in a 100-column terminal with six panes; wants to reclaim vertical space by hiding admin buttons until needed.
- **S-02 Natural typing**: operator clicks the InputBox and types `cd src/; ls !*.py` — every character including `;` and `!` must reach the agent in order.
- **S-03 Fast start**: operator clicks Start Group; expects all six agents to come online in roughly the time it takes one agent to start, not six times that.
- **S-04 Reliable orchestration**: operator starts a group with the `standard` workflow; expects the orchestrator to dispatch to PM within 10 seconds and the PM to receive the prompt immediately.
- **S-05 Skill-driven pipeline**: operator expects the orchestrator to tell each worker "invoke /req-1-analyze" (or whichever `/req-*` skill is appropriate) so the entire `/req` pipeline runs automatically across the agent chain.
- **S-06 Dispatch debugging**: operator runs the platform, sees no dispatch reaching workers, opens the log and immediately finds a warning line indicating whether parsing failed or `send_keys` failed.

## 3. Functional Requirements

### F-01 Collapsible admin controls row

- Main flow: the admin controls row (`Pause`, `Resume`, `Edit`, `Restart`, `Delete`) is hidden by default. A toggle button labelled `⋯ admin` is added to the AgentPane header to the right of the status badge. Clicking it adds/removes a `collapsed` CSS class on the admin row; the collapsed class sets `display: none`.
- The `Enter` button (Attach trigger) stays visible in the header row next to the toggle, since it is the primary interaction affordance.
- Quick keyboard and InputBox remain always visible below the preview — they are used every session and are not "admin" actions.
- Error handling: none — pure CSS toggle.
- Edge cases: when `update_status` changes the pane to `active`, the `Enter` button becomes visible; collapsed admin row state is independent.

### F-02 Punctuation forwarding via `event.character`

- Main flow: a new helper `key_forwarding.tmux_args_for_key(event)` is introduced. It prefers `event.key` for named special keys (Enter, Tab, arrows, ctrl combinations — because those give precise tmux names) and falls back to `event.character` for anything else (printable chars including punctuation). The InputBox's `on_key` handler calls this helper instead of calling `textual_to_tmux(event.key)` directly.
- Character priority rules:
  1. If `event.key` is in the named `_SPECIAL` set (e.g. `enter`, `tab`, `up`, `escape`) → use `event.key`.
  2. If `event.key` starts with `ctrl+` → use `event.key`.
  3. Else if `event.character` is a single printable character → use `event.character`.
  4. Else if `event.character` is a multi-character printable string → use `event.character`.
  5. Else → fall through to `textual_to_tmux(event.key)` (which handles single-char `event.key` for older Textual versions).
- All five punctuation families must round-trip correctly: alphabetic ASCII, digits, shift-modified punctuation (`!@#$%^&*()_+`), brace and bracket punctuation (`[](){};:'",<.>/?`), backslash and pipe (`\|`), grave and tilde (`` ` ~``).
- Error handling: unrecognised keys continue to return `None` and are dropped silently.
- Edge cases: Chinese / emoji input via IME → `event.character` carries the composed string → forwarded via the multi-char unicode branch.

### F-03 Concurrent `start_group` / `resume_group` / `stop_group`

- Main flow:
  - `start_group(group_id)` fetches the worker list and launches all workers via `asyncio.gather(*[start_agent_session(w) for w in workers], return_exceptions=True)`. Each `start_agent_session` call still runs the F-04 readiness poll individually. After `gather` returns, the supervisor verifies that all non-exception results are active and only then starts the orchestrator (orchestrator start is still awaited serially because it depends on the roster with live pane IDs).
  - `resume_group(group_id)` does the same: workers first in parallel, orchestrator last.
  - `stop_group(group_id)` stops all sessions in parallel via `asyncio.gather(*[stop_agent_session(s) for s in sessions], return_exceptions=True)`.
- Exception handling: `return_exceptions=True` ensures a single failing agent does not cancel the others. For each exception, the supervisor logs it and marks the corresponding agent `degraded`; healthy agents remain active.
- TUI ordering: `AgentStatusChanged` messages may arrive in any order. The TUI already handles out-of-order status updates because each AgentPane is independent.
- Error handling: if all workers fail, the orchestrator is not started and the user is notified via toast "Group start failed — see log".
- Edge cases: when an agent's session already exists (resume path), the parallel start uses `resume_session_id` per agent — no cross-agent state sharing, no race condition.

### F-04 Dispatch reliability

Four cooperating fixes:

#### F-04a Parser leniency — accept self-closing dispatch form

- Main flow: `orchestrator.parse_latest_dispatch` now accepts two shapes:
  1. **Full form** (existing): `<<DISPATCH role="X" text="...">>…<</DISPATCH>>`
  2. **Self-closing form** (new): `<<DISPATCH role="X" text="...">>` or `<<DISPATCH role="X" text="..."/>>` — no closing tag, no body.
- Parsing order: try full form first (preserves backward compatibility); fall back to self-closing if no full match.
- The self-closing regex is more forgiving about trailing whitespace, leading/trailing slashes, and optional body.
- Edge cases: if the orchestrator mixes forms across dispatches in the same turn, each dispatch is parsed independently — the supervisor still uses content-signature dedup from REQ-014.

#### F-04b `send_keys` — drop the cat fallback

- Main flow: the existing "temp file + cat" long-payload fallback in `SessionManager.send_keys` is **deleted**. All payloads — short and long — are sent directly via `tmux send-keys -t {pane} {text} Enter`. The argv size limit on all target OSes is well above the 50 KB `_sanitize_payload` cap.
- Rationale: the cat fallback was designed for shell panes; in a Claude CLI pane the text `cat /path/to/file` is sent as a chat message, not executed, so the dispatched content never reaches the agent.
- Error handling: `send_keys` logs at warning level when tmux returns non-zero.
- Edge cases: the `DIRECT_SEND_MAX_LEN` constant (previously 200) is kept in `shared/config.py` for backwards compatibility but its meaning changes from "threshold" to "logging hint" (unused at runtime).

#### F-04c Dispatch text newline normalisation

- Main flow: before the dispatch_loop calls `send_keys(worker_pane, dispatch.text)`, it strips embedded line breaks via `dispatch.text.replace("\r\n", " ").replace("\n", " ")`. The resulting single-line text is sent as one argv token followed by Enter, avoiding tmux interpreting `\n` as a premature submission.
- Rationale: tmux `send-keys "multi\nline"` sends a literal newline which the agent's terminal interprets as Enter, submitting only the first line to Claude.
- Edge cases: the orchestrator's dispatch text is typically single-line anyway (the `text=` attribute in the dispatch block is a quoted single-line value). This fix is defensive against LLM output variability.

#### F-04d Diagnostic logging for parse failures

- Main flow: in the dispatch_loop, when `parse_latest_dispatch` returns `None` but the captured orchestrator pane text contains the literal substring `<<DISPATCH`, a warning log is emitted: `orchestrator pane contains <<DISPATCH but parser returned None — text was: <tail>`. To avoid log spam, the same tail is not logged twice in a row (signature dedup via a new `_last_parse_warning` field on the supervisor).
- Rationale: the v15 dispatch_loop silently skipped unparseable dispatches, making it impossible to tell whether the issue was "orchestrator not emitting anything" vs "emitting but format wrong".
- Edge cases: the tail is truncated to 200 characters to keep log lines readable.

### F-05 Orchestrator must command `/req-*` skill invocation

- Main flow: the `Step` dataclass in `backend/workflows.py` gains a new optional field `skill: Optional[str] = None`. The three built-in workflows are updated so every non-user step carries the corresponding `/req-*` skill name:
  - `STANDARD`:
    - PM: `skill="req-1-analyze"`
    - Tech Director: `skill="req-2-tech"`
    - Developer: `skill="req-3-code"` (with a note that the developer should chain through `/req-4-security`, `/req-5-cleanup`, `/req-6-review`, `/req-7-verify` afterwards)
    - Tester: `skill="req-7-verify"`
    - User: `skill=None` (human review)
  - `PROTOTYPE`:
    - Developer: `skill="req-3-code"`
    - User: `skill=None`
  - `RESEARCH`:
    - PM: `skill="req-1-analyze"`
    - Tech Director: `skill="req-2-tech"`
    - User: `skill=None`
- `render_for_orchestrator` is updated to include `→ must invoke /req-X-Y` on each step line that carries a skill, so the orchestrator's system prompt describes the expected skill per role.
- Orchestrator system prompt (`_DEFAULT_TEMPLATES["orchestrator"]`) gains a new dedicated section titled `## 技能调用规则` that explains:
  1. Every workflow step may carry a `/req-*` skill name.
  2. When dispatching to a role, the orchestrator MUST mention the skill in the dispatch `text` attribute.
  3. Recommended format: `text="Please invoke /req-1-analyze with this goal: {description}. When done, output <<TASK_DONE>> on its own line."`
- Worker templates (`product_manager`, `tech_director`, `developer`, `tester`) gain a new section stating that if the dispatched prompt mentions a `/req-*` skill, the worker MUST invoke that skill as a slash command in its own session before producing the final artefact. The `user` and `custom` templates are unaffected.
- `_TEMPLATE_VERSION` bumps from 5 to 6 so the bundled templates propagate automatically on next start.
- Error handling: if a workflow step has `skill=None`, the orchestrator prompt line shows `(no skill)` and the orchestrator knows not to fabricate a skill invocation.
- Edge cases: users who have customised their role templates will have their edits overwritten by the bump (same known limitation as every previous template bump; documented in `Out of Scope`).

## 4. Non-functional Requirements

- All 383 existing tests (REQ-012 v2 + REQ-014 + REQ-015) must continue to pass.
- New tests required:
  - `tests/test_key_forwarding.py`: tests for `tmux_args_for_key(event)` with fake events carrying different `key` / `character` combinations. At least 15 tests covering special keys, ctrl combinations, printable chars, punctuation, unicode.
  - `tests/test_agent_pane.py`: test that the admin controls toggle button exists and that pressing it adds/removes the `collapsed` class. Test that typing `!` via pilot forwards `!` to the agent (regression guard for F-02).
  - `tests/test_supervisor_concurrency.py` (new): tests that `start_group`, `stop_group`, `resume_group` call `asyncio.gather` so multiple agent lifecycles run in parallel. Verify by instrumenting the fake SessionManager with a per-call sleep and asserting total wall time is below N×single-call time.
  - `tests/test_orchestrator.py`: tests for the new self-closing dispatch form (F-04a). Tests for the full form continue to pass.
  - `tests/test_dispatch_integration.py`: test that dispatch text with embedded `\n` is normalised before send_keys. Test that parse failure with `<<DISPATCH` substring triggers a diagnostic log (captured via caplog fixture).
  - `tests/test_workflows.py`: test that every non-user step in the three built-in workflows has a non-None `skill` field. Test `render_for_orchestrator` output includes the skill name on the step line.
  - `tests/test_repository.py`: test that `_TEMPLATE_VERSION` has bumped and the orchestrator template mentions `/req-*` and the word `skill`.
- No new runtime dependencies.
- No schema changes.

## 5. Out of Scope

- Per-worker custom skill mapping UI — the skills are hardcoded in the built-in workflows.
- Multi-line dispatch text support — v1 strips newlines; future REQ may add bracketed-paste support if needed.
- Parallel dispatch of multiple workers from the orchestrator — still sequential per REQ-012 v2 decision.
- Custom toggle keybinding for the admin row — mouse click only in v1.
- Template overwrite preservation — the `_TEMPLATE_VERSION` bump overwrites user customisations, same known limitation as every previous REQ.
- Input history / paste buffer in the InputBox.
- Real terminal emulator via pyte / textual-terminal — same decision as REQ-015.

## 6. Acceptance Criteria

| ID | Feature | Condition | Expected Result |
|:---|:---|:---|:---|
| AC-01 | F-01 | Start the TUI; create a group; observe an AgentPane | Admin row (Pause/Resume/Edit/Restart/Delete) is NOT visible by default; a `⋯ admin` toggle button is present in the header |
| AC-02 | F-01 | Click the `⋯ admin` toggle | Admin row becomes visible |
| AC-03 | F-01 | Click the toggle again | Admin row is hidden |
| AC-04 | F-02 | Focus the InputBox; type `!` | The agent's tmux pane receives the character `!` (verified via send_raw_keys call log) |
| AC-05 | F-02 | Focus the InputBox; type `@`, `#`, `$`, `%`, `^`, `&`, `*`, `(`, `)`, `-`, `_`, `=`, `+`, `[`, `]`, `{`, `}`, `;`, `:`, `'`, `"`, `,`, `.`, `<`, `>`, `/`, `?`, `\|`, `~`, `` ` `` | Every character is forwarded verbatim |
| AC-06 | F-02 | `tmux_args_for_key(event)` called with `event.key="exclamation_mark"`, `event.character="!"` | Returns `["!"]` |
| AC-07 | F-02 | `tmux_args_for_key(event)` called with `event.key="enter"`, `event.character=None` | Returns `["Enter"]` (event.key takes precedence for named specials) |
| AC-08 | F-03 | Instrument `FakeSessionManager.start_agent_session` with a 100 ms sleep; call `supervisor.start_group` with a group of 5 workers + 1 orchestrator | Total time < 300 ms (proof of parallelism) — if sequential it would take 600 ms |
| AC-09 | F-03 | Same instrumentation for `stop_agent_session`; call `supervisor.stop_group` | Total stop time shorter than sequential equivalent |
| AC-10 | F-03 | One worker's `start_agent_session` raises an exception; other workers succeed | Exception is caught, the failing worker is marked `degraded`, other workers remain active, the orchestrator does NOT start |
| AC-11 | F-04a | `parse_latest_dispatch('<<DISPATCH role="developer" text="do X">>')` (no closing tag) | Returns a Dispatch with role="developer", text="do X" |
| AC-12 | F-04a | `parse_latest_dispatch('<<DISPATCH role="developer" text="do X"/>>')` (self-closing slash) | Returns the same Dispatch |
| AC-13 | F-04a | `parse_latest_dispatch('<<DISPATCH role="developer" text="do X">>body<</DISPATCH>>')` (full form) | Returns the same Dispatch (backward compat) |
| AC-14 | F-04b | Call `send_keys` with a 1000-character payload | tmux send-keys is called directly (no temp file, no cat) |
| AC-15 | F-04b | grep `src/` for `cat ` with quoted path or `_cleanup_temp` related to `agent_msg_` | Zero matches in send_keys long-payload path |
| AC-16 | F-04c | Dispatch text is `"line1\nline2\nline3"`; dispatch_loop forwards to worker | The `send_keys` call receives `"line1 line2 line3"` (newlines replaced with spaces) |
| AC-17 | F-04d | Orchestrator pane contains `<<DISPATCH` but the parser can't extract a valid dispatch | A warning log line is emitted containing the literal `<<DISPATCH` and a truncated tail of the pane text |
| AC-18 | F-05 | `workflows.STANDARD.steps[0].skill` | Equals `"req-1-analyze"` |
| AC-19 | F-05 | `render_for_orchestrator(STANDARD, roster)` output | Contains the substring `/req-1-analyze` (or similar per-step skill references) |
| AC-20 | F-05 | `repo.get_orchestrator_template()` after template version bump | Contains the section title `技能调用规则` AND the string `/req-` appears at least once |
| AC-21 | F-05 | PM / Tech Director / Developer / Tester templates | Each contains instructions to invoke `/req-*` skills when asked |
| AC-22 | F-05 | `repository._TEMPLATE_VERSION` | Equals `6` |
| AC-23 | NFR | Run pytest | All previous tests (383) continue to pass; new tests bring the total to at least 420 |

## 7. Change Log

| Version | Date | Changes | Affected Scope | Reason |
|:---|:---|:---|:---|:---|
| v1 | 2026-04-08 | Initial version — 5-issue polish pass: (F-01) collapsible admin controls row with toggle button; (F-02) punctuation forwarding via new `tmux_args_for_key` helper using `event.character`; (F-03) concurrent start/stop/resume_group via `asyncio.gather`; (F-04a) parse_latest_dispatch accepts self-closing form; (F-04b) send_keys cat fallback deleted (broken in Claude CLI panes); (F-04c) dispatch text newline normalisation; (F-04d) diagnostic log when `<<DISPATCH` is present but parser returns None; (F-05) Step.skill field, three built-in workflows updated with `/req-*` skill hints, orchestrator template gains `## 技能调用规则` section mandating skill invocation instructions, worker templates updated to require skill invocation on receipt, `_TEMPLATE_VERSION` bumped to 6. | ALL | User reported 5 concrete issues after live REQ-015 testing |
