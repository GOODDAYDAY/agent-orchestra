"""REQ-015 — Tests for the AgentPane widget and the InputBox focus catcher.

These tests stay close to the unit/widget boundary: they instantiate widgets
inside a tiny Textual `App` via `App.run_test()` so on_key handlers and focus
state actually fire, but they avoid full integration with the supervisor / repo.
"""
from __future__ import annotations

import pytest

from textual.app import App, ComposeResult
from textual.widgets import Button

from agent_management.backend.models import Agent, AgentRole, AgentStatus
from agent_management.frontend.agent_pane import (
    AgentPane,
    InputBox,
    QUICK_KEYS,
    QUICK_KEY_SPECS,
)


# ---- QUICK_KEY_SPECS coverage -----------------------------------------------

class TestQuickKeySpecs:
    def test_every_quick_key_has_a_spec(self):
        for _label, key_id in QUICK_KEYS:
            assert key_id in QUICK_KEY_SPECS, (
                f"QUICK_KEYS row '{key_id}' has no spec in QUICK_KEY_SPECS"
            )

    def test_continue_sends_text_with_enter(self):
        assert QUICK_KEY_SPECS["continue"] == ["continue", "Enter"]

    def test_y_sends_y_with_enter(self):
        assert QUICK_KEY_SPECS["y"] == ["y", "Enter"]

    def test_n_sends_n_with_enter(self):
        assert QUICK_KEY_SPECS["n"] == ["n", "Enter"]

    def test_escape_sends_named_escape(self):
        # Must be the named tmux key, not the literal "Esc" text
        assert QUICK_KEY_SPECS["escape"] == ["Escape"]

    def test_ctrl_c_sends_named_c_c(self):
        assert QUICK_KEY_SPECS["ctrl-c"] == ["C-c"]

    def test_arrows_send_named_keys(self):
        assert QUICK_KEY_SPECS["up"] == ["Up"]
        assert QUICK_KEY_SPECS["down"] == ["Down"]

    def test_ctrl_d_sends_named_c_d(self):
        assert QUICK_KEY_SPECS["ctrl-d"] == ["C-d"]

    def test_eight_quick_keys_total(self):
        assert len(QUICK_KEYS) == 8
        assert len(QUICK_KEY_SPECS) == 8

    def test_no_orphan_specs(self):
        # Every spec should be referenced by exactly one QUICK_KEYS entry
        ids_in_keys = {key_id for _label, key_id in QUICK_KEYS}
        ids_in_specs = set(QUICK_KEY_SPECS.keys())
        assert ids_in_keys == ids_in_specs


# ---- InputBox direct unit tests (without a running app) ---------------------

class _HostApp(App):
    """Minimal Textual host so InputBox / AgentPane can be mounted in tests."""

    def __init__(self, child_factory) -> None:
        super().__init__()
        self._child_factory = child_factory
        self.forwarded: list[tuple[str, list[str]]] = []
        self.attach_requests: list[str] = []

    def compose(self) -> ComposeResult:
        yield self._child_factory()

    def on_input_box_key_forwarded(self, message: InputBox.KeyForwarded) -> None:
        self.forwarded.append((message.agent_id, list(message.spec)))

    def on_agent_pane_key_forwarded(self, message: AgentPane.KeyForwarded) -> None:
        self.forwarded.append((message.agent_id, list(message.spec)))

    def on_agent_pane_attach_requested(
        self, message: AgentPane.AttachRequested
    ) -> None:
        self.attach_requests.append(message.agent_id)


class TestInputBoxKeyForwarding:
    async def test_typing_a_letter_forwards_via_message(self):
        app = _HostApp(lambda: InputBox(agent_id="A1", id="ib"))
        async with app.run_test() as pilot:
            ib = app.query_one("#ib", InputBox)
            ib.focus()
            await pilot.press("a")
            assert ("A1", ["a"]) in app.forwarded

    async def test_enter_forwards_named_enter(self):
        app = _HostApp(lambda: InputBox(agent_id="A1", id="ib"))
        async with app.run_test() as pilot:
            ib = app.query_one("#ib", InputBox)
            ib.focus()
            await pilot.press("enter")
            assert ("A1", ["Enter"]) in app.forwarded

    async def test_tab_forwards_named_tab(self):
        app = _HostApp(lambda: InputBox(agent_id="A1", id="ib"))
        async with app.run_test() as pilot:
            ib = app.query_one("#ib", InputBox)
            ib.focus()
            await pilot.press("tab")
            assert ("A1", ["Tab"]) in app.forwarded

    async def test_ctrl_c_forwards_C_c(self):
        app = _HostApp(lambda: InputBox(agent_id="A1", id="ib"))
        async with app.run_test() as pilot:
            ib = app.query_one("#ib", InputBox)
            ib.focus()
            await pilot.press("ctrl+c")
            assert ("A1", ["C-c"]) in app.forwarded

    async def test_unrecognised_key_is_dropped_silently(self):
        app = _HostApp(lambda: InputBox(agent_id="A1", id="ib"))
        async with app.run_test() as pilot:
            ib = app.query_one("#ib", InputBox)
            ib.focus()
            # ctrl+enter is not in the mapping (multi-char ctrl combo)
            await pilot.press("ctrl+enter")
            # No forwarded message produced for the dropped key
            assert all(spec != ["C-enter"] for _aid, spec in app.forwarded)

    async def test_arrow_keys_forwarded(self):
        app = _HostApp(lambda: InputBox(agent_id="A1", id="ib"))
        async with app.run_test() as pilot:
            ib = app.query_one("#ib", InputBox)
            ib.focus()
            await pilot.press("up")
            await pilot.press("down")
            specs = [spec for _aid, spec in app.forwarded]
            assert ["Up"] in specs
            assert ["Down"] in specs


# ---- AgentPane button keyboard wiring ---------------------------------------

def _make_agent(role=AgentRole.developer) -> Agent:
    return Agent(
        name="Test Agent",
        role=role,
        working_dir="/tmp",
        status=AgentStatus.active,
    )


class TestAgentPaneButtonKeyboard:
    async def test_y_button_posts_key_forwarded_with_y_enter(self):
        agent = _make_agent()
        app = _HostApp(lambda: AgentPane(agent=agent))
        async with app.run_test() as pilot:
            pane = app.query_one(AgentPane)
            btn = pane.query_one(f"#qk-y-{agent.id}", Button)
            btn.press()
            await pilot.pause()
            assert any(
                aid == agent.id and spec == ["y", "Enter"]
                for aid, spec in app.forwarded
            )

    async def test_escape_button_posts_named_escape(self):
        agent = _make_agent()
        app = _HostApp(lambda: AgentPane(agent=agent))
        async with app.run_test() as pilot:
            pane = app.query_one(AgentPane)
            btn = pane.query_one(f"#qk-escape-{agent.id}", Button)
            btn.press()
            await pilot.pause()
            assert any(
                aid == agent.id and spec == ["Escape"]
                for aid, spec in app.forwarded
            )

    async def test_ctrl_c_button_posts_C_c(self):
        agent = _make_agent()
        app = _HostApp(lambda: AgentPane(agent=agent))
        async with app.run_test() as pilot:
            pane = app.query_one(AgentPane)
            btn = pane.query_one(f"#qk-ctrl-c-{agent.id}", Button)
            btn.press()
            await pilot.pause()
            assert any(
                aid == agent.id and spec == ["C-c"]
                for aid, spec in app.forwarded
            )

    async def test_continue_button_posts_continue_with_enter(self):
        agent = _make_agent()
        app = _HostApp(lambda: AgentPane(agent=agent))
        async with app.run_test() as pilot:
            pane = app.query_one(AgentPane)
            btn = pane.query_one(f"#qk-continue-{agent.id}", Button)
            btn.press()
            await pilot.pause()
            assert any(
                aid == agent.id and spec == ["continue", "Enter"]
                for aid, spec in app.forwarded
            )

    async def test_all_eight_buttons_present(self):
        agent = _make_agent()
        app = _HostApp(lambda: AgentPane(agent=agent))
        async with app.run_test() as pilot:
            pane = app.query_one(AgentPane)
            for _label, key_id in QUICK_KEYS:
                btn = pane.query_one(f"#qk-{key_id}-{agent.id}", Button)
                assert btn is not None


# ---- AgentPane removed widgets ----------------------------------------------

class TestRemovedWidgets:
    async def test_no_send_button_in_pane(self):
        agent = _make_agent()
        app = _HostApp(lambda: AgentPane(agent=agent))
        async with app.run_test():
            pane = app.query_one(AgentPane)
            with pytest.raises(Exception):
                pane.query_one(f"#btn-send-{agent.id}")

    async def test_no_legacy_input_in_pane(self):
        agent = _make_agent()
        app = _HostApp(lambda: AgentPane(agent=agent))
        async with app.run_test():
            pane = app.query_one(AgentPane)
            with pytest.raises(Exception):
                pane.query_one(f"#inp-send-{agent.id}")

    def test_send_requested_message_class_removed(self):
        # The OLD AgentPane.SendRequested message class must be gone.
        assert not hasattr(AgentPane, "SendRequested")


# ---- AgentPane orchestrator marker ------------------------------------------

# ---- REQ-016 F-01: collapsible admin row -----------------------------------

class TestAdminRowCollapse:
    async def test_admin_row_collapsed_by_default(self):
        agent = _make_agent()
        app = _HostApp(lambda: AgentPane(agent=agent))
        async with app.run_test():
            pane = app.query_one(AgentPane)
            controls = pane.query_one(f"#controls-{agent.id}")
            assert "collapsed" in controls.classes

    async def test_admin_toggle_button_present_in_header(self):
        agent = _make_agent()
        app = _HostApp(lambda: AgentPane(agent=agent))
        async with app.run_test():
            pane = app.query_one(AgentPane)
            btn = pane.query_one(f"#btn-admin-toggle-{agent.id}", Button)
            assert btn is not None

    async def test_clicking_toggle_expands_controls(self):
        agent = _make_agent()
        app = _HostApp(lambda: AgentPane(agent=agent))
        async with app.run_test() as pilot:
            pane = app.query_one(AgentPane)
            controls = pane.query_one(f"#controls-{agent.id}")
            assert "collapsed" in controls.classes
            btn = pane.query_one(f"#btn-admin-toggle-{agent.id}", Button)
            btn.press()
            await pilot.pause()
            assert "collapsed" not in controls.classes

    async def test_clicking_toggle_twice_collapses_again(self):
        agent = _make_agent()
        app = _HostApp(lambda: AgentPane(agent=agent))
        async with app.run_test() as pilot:
            pane = app.query_one(AgentPane)
            controls = pane.query_one(f"#controls-{agent.id}")
            btn = pane.query_one(f"#btn-admin-toggle-{agent.id}", Button)
            btn.press()
            await pilot.pause()
            btn.press()
            await pilot.pause()
            assert "collapsed" in controls.classes

    async def test_enter_button_in_header_not_admin_row(self):
        agent = _make_agent()
        app = _HostApp(lambda: AgentPane(agent=agent))
        async with app.run_test():
            pane = app.query_one(AgentPane)
            enter_btn = pane.query_one("#enter-agent", Button)
            # The enter button's parent should be the pane-header Horizontal,
            # not the pane-controls row.
            parent_classes = set(enter_btn.parent.classes) if enter_btn.parent else set()
            assert "pane-header" in parent_classes
            assert "pane-controls" not in parent_classes


# ---- REQ-016 F-02: punctuation forwarding regression guard -----------------

class TestInputBoxPunctuationForwarding:
    async def test_exclamation_mark_forwarded_via_character(self):
        """Regression guard: shift-modified punctuation must be forwarded
        even when Textual reports event.key as a named identifier and
        event.character holds the real character."""
        from agent_management.frontend.key_forwarding import tmux_args_for_key
        from types import SimpleNamespace
        # Synthesize what Textual would emit on Shift+1
        fake = SimpleNamespace(key="exclamation_mark", character="!")
        assert tmux_args_for_key(fake) == ["!"]

    async def test_punctuation_family_via_character(self):
        from agent_management.frontend.key_forwarding import tmux_args_for_key
        from types import SimpleNamespace
        for ch in "!@#$%^&*()_+-=[]{};:'\",.<>/?\\|`~":
            fake = SimpleNamespace(key=f"synthetic-{ch}", character=ch)
            assert tmux_args_for_key(fake) == [ch], f"failed for {ch!r}"

    async def test_input_box_uses_tmux_args_for_key(self):
        """When the user types 'a' into the input box (both key and character
        set), the InputBox must forward the character via the new helper."""
        app = _HostApp(lambda: InputBox(agent_id="A1", id="ib"))
        async with app.run_test() as pilot:
            ib = app.query_one("#ib", InputBox)
            ib.focus()
            await pilot.press("a")
            assert ("A1", ["a"]) in app.forwarded


class TestAgentPaneRoleMarker:
    @staticmethod
    def _label_text(label) -> str:
        """Render a Textual Label to a plain string for assertion."""
        rendered = label.render()
        # Label.render() may return a Rich Text or a plain str
        return str(rendered)

    async def test_orchestrator_pane_shows_marker(self):
        agent = _make_agent(role=AgentRole.orchestrator)
        app = _HostApp(lambda: AgentPane(agent=agent))
        async with app.run_test():
            pane = app.query_one(AgentPane)
            pane.update_status(AgentStatus.active)
            from textual.widgets import Label
            marker = pane.query_one(f"#role-marker-{agent.id}", Label)
            assert "orchestrator" in self._label_text(marker)

    async def test_worker_pane_has_blank_marker(self):
        agent = _make_agent(role=AgentRole.developer)
        app = _HostApp(lambda: AgentPane(agent=agent))
        async with app.run_test():
            pane = app.query_one(AgentPane)
            pane.update_status(AgentStatus.active)
            from textual.widgets import Label
            marker = pane.query_one(f"#role-marker-{agent.id}", Label)
            assert "orchestrator" not in self._label_text(marker)
