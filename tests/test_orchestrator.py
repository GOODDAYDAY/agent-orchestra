"""REQ-012 v2 — Unit tests for backend.orchestrator pure functions.

These tests must run without tmux, without subprocess, without asyncio,
and without a live LLM. The orchestrator module is intentionally pure
so that the dispatch parser and completion detector are deterministic
and trivially testable.
"""
from __future__ import annotations

from agent_management.backend.orchestrator import (
    CompletionLayer,
    detect_completion,
    is_workflow_abort,
    is_workflow_complete,
    parse_latest_dispatch,
    validate_dispatch_text,
)


# ---- parse_latest_dispatch ---------------------------------------------------

class TestParseLatestDispatch:
    def test_well_formed_single_dispatch(self):
        text = '<<DISPATCH role="developer" text="implement X">>\n<</DISPATCH>>'
        d = parse_latest_dispatch(text)
        assert d is not None
        assert d.role == "developer"
        assert d.text == "implement X"
        assert d.end_offset == len(text)

    def test_no_dispatch_returns_none(self):
        assert parse_latest_dispatch("nothing here") is None

    def test_multiple_dispatches_returns_last(self):
        text = (
            '<<DISPATCH role="pm" text="first">><</DISPATCH>>\n'
            '<<DISPATCH role="developer" text="second">><</DISPATCH>>'
        )
        d = parse_latest_dispatch(text)
        assert d is not None
        assert d.role == "developer"
        assert d.text == "second"

    def test_after_offset_skips_consumed_dispatches(self):
        text = (
            '<<DISPATCH role="pm" text="first">><</DISPATCH>>\n'
            '<<DISPATCH role="developer" text="second">><</DISPATCH>>'
        )
        first = parse_latest_dispatch(text)
        # Now consume up to first dispatch's end and look for the second
        d = parse_latest_dispatch(text, after_offset=text.index('<</DISPATCH>>') + len('<</DISPATCH>>'))
        assert d is not None
        assert d.role == "developer"
        assert first.role == "developer"  # without offset, "latest" is still developer

    def test_escaped_quotes_in_text(self):
        text = '<<DISPATCH role="developer" text="say \\"hi\\"">>\n<</DISPATCH>>'
        d = parse_latest_dispatch(text)
        assert d is not None
        assert d.text == 'say "hi"'

    def test_role_is_lowercased(self):
        text = '<<DISPATCH role="developer" text="x">><</DISPATCH>>'
        assert parse_latest_dispatch(text).role == "developer"

    def test_malformed_no_closing_tag_returns_none(self):
        text = '<<DISPATCH role="developer" text="x">> body without close'
        assert parse_latest_dispatch(text) is None

    def test_dotall_body_can_span_lines(self):
        # The dispatch block body itself can span multiple lines; the text=
        # attribute uses simple backslash-quote escaping (no \n shorthand).
        text = '<<DISPATCH role="pm" text="single line text">>\n  body\n  more body\n<</DISPATCH>>'
        d = parse_latest_dispatch(text)
        assert d is not None
        assert d.text == "single line text"
        assert "more body" in d.raw


# ---- workflow control markers ------------------------------------------------

class TestWorkflowControl:
    def test_workflow_complete_detected(self):
        assert is_workflow_complete("foo\n<<WORKFLOW_COMPLETE>>\nbar")

    def test_workflow_complete_must_be_at_line_start(self):
        assert not is_workflow_complete("inline <<WORKFLOW_COMPLETE>> nope")

    def test_workflow_abort_returns_reason(self):
        text = '<<WORKFLOW_ABORT reason="test loop exceeded"/>>'
        assert is_workflow_abort(text) == "test loop exceeded"

    def test_workflow_abort_none_when_absent(self):
        assert is_workflow_abort("nothing") is None

    def test_workflow_abort_escaped_reason(self):
        text = '<<WORKFLOW_ABORT reason="quoted \\"foo\\""/>>'
        assert is_workflow_abort(text) == 'quoted "foo"'


# ---- validate_dispatch_text --------------------------------------------------

class TestValidateDispatchText:
    def test_clean_text_passes(self):
        assert validate_dispatch_text("normal prompt body") is None

    def test_task_done_marker_rejected(self):
        err = validate_dispatch_text("here is <<TASK_DONE>> embedded")
        assert err is not None
        assert "<<TASK_DONE>>" in err

    def test_workflow_complete_rejected(self):
        err = validate_dispatch_text("here is <<WORKFLOW_COMPLETE>> in text")
        assert err is not None

    def test_workflow_abort_prefix_rejected(self):
        err = validate_dispatch_text('something <<WORKFLOW_ABORT reason="x">>')
        assert err is not None


# ---- detect_completion -------------------------------------------------------

class TestDetectCompletion:
    def test_marker_layer_extracts_artifact(self):
        text = "the work is done\nmore output\n<<TASK_DONE>>"
        result = detect_completion(
            pane_text=text,
            dispatch_end_offset=0,
            last_change_at=100.0,
            dispatch_at=0.0,
            now=10.0,
        )
        assert result.layer == CompletionLayer.marker
        assert "the work is done" in result.artifact
        assert "<<TASK_DONE>>" not in result.artifact
        assert result.tests_failed is False

    def test_marker_with_tests_failed(self):
        text = "test report\n<<TESTS_FAILED>>\n<<TASK_DONE>>"
        result = detect_completion(
            pane_text=text,
            dispatch_end_offset=0,
            last_change_at=100.0,
            dispatch_at=0.0,
            now=10.0,
        )
        assert result.layer == CompletionLayer.marker
        assert result.tests_failed is True

    def test_marker_truncates_at_first_occurrence(self):
        text = "stage 1\n<<TASK_DONE>>\nbut then more output"
        result = detect_completion(
            pane_text=text,
            dispatch_end_offset=0,
            last_change_at=100.0,
            dispatch_at=0.0,
            now=10.0,
        )
        assert result.layer == CompletionLayer.marker
        assert result.artifact == "stage 1"
        assert "more output" not in result.artifact

    def test_marker_must_be_at_line_start(self):
        text = "inline <<TASK_DONE>> nope"
        result = detect_completion(
            pane_text=text,
            dispatch_end_offset=0,
            last_change_at=100.0,
            dispatch_at=0.0,
            now=10.0,
        )
        assert result.layer != CompletionLayer.marker

    def test_silence_layer_after_timeout(self):
        text = "some output but no marker"
        result = detect_completion(
            pane_text=text,
            dispatch_end_offset=0,
            last_change_at=0.0,    # no changes since dispatch
            dispatch_at=0.0,
            now=70.0,              # 70s elapsed, > silence_timeout of 60
            silence_timeout=60.0,
            stall_timeout=600.0,
        )
        assert result.layer == CompletionLayer.silence
        assert "some output but no marker" in result.artifact

    def test_silence_does_not_fire_on_empty_pane(self):
        result = detect_completion(
            pane_text="   \n   ",
            dispatch_end_offset=0,
            last_change_at=0.0,
            dispatch_at=0.0,
            now=70.0,
        )
        assert result.layer != CompletionLayer.silence

    def test_stall_layer_after_global_timeout(self):
        result = detect_completion(
            pane_text="",
            dispatch_end_offset=0,
            last_change_at=0.0,
            dispatch_at=0.0,
            now=700.0,             # > stall_timeout of 600
            silence_timeout=60.0,
            stall_timeout=600.0,
        )
        assert result.layer == CompletionLayer.stall

    def test_pending_layer_when_nothing_fires(self):
        result = detect_completion(
            pane_text="brief output",
            dispatch_end_offset=0,
            last_change_at=10.0,
            dispatch_at=0.0,
            now=15.0,
            silence_timeout=60.0,
            stall_timeout=600.0,
        )
        assert result.layer == CompletionLayer.pending

    def test_dispatch_offset_excludes_history(self):
        # The dispatch text itself contained "<<TASK_DONE>>" — but dispatch_end_offset
        # should restrict the marker search to text *after* it.
        full = "old content with <<TASK_DONE>> here\n<<<dispatch end here>>>\nworker output"
        offset = full.index("<<<dispatch end here>>>") + len("<<<dispatch end here>>>")
        result = detect_completion(
            pane_text=full,
            dispatch_end_offset=offset,
            last_change_at=100.0,
            dispatch_at=0.0,
            now=10.0,
        )
        # No new marker after offset → pending
        assert result.layer == CompletionLayer.pending

    def test_marker_precedence_over_silence(self):
        text = "output\n<<TASK_DONE>>"
        result = detect_completion(
            pane_text=text,
            dispatch_end_offset=0,
            last_change_at=0.0,    # ancient last change
            dispatch_at=0.0,
            now=70.0,              # silence would fire too
            silence_timeout=60.0,
            stall_timeout=600.0,
        )
        assert result.layer == CompletionLayer.marker
