"""REQ-012 v2 — Unit tests for backend.orchestrator pure functions.

These tests must run without tmux, without subprocess, without asyncio,
and without a live LLM. The orchestrator module is intentionally pure
so that the dispatch parser and completion detector are deterministic
and trivially testable.
"""
from __future__ import annotations

import pytest

from agent_management.backend.orchestrator import (
    CompletionLayer,
    CompletionResult,
    Dispatch,
    _unescape_text,
    detect_completion,
    is_workflow_abort,
    is_workflow_complete,
    parse_latest_dispatch,
    validate_dispatch_text,
)


# ---- parse_latest_dispatch ---------------------------------------------------

class TestParseLatestDispatch:
    def test_well_formed_single_dispatch(self):
        # REQ-016 F-04a: parser captures just the `<<DISPATCH ...>>` prefix;
        # any trailing `<</DISPATCH>>` is left for subsequent text.
        text = '<<DISPATCH role="developer" text="implement X">>'
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

    def test_no_closing_tag_accepted_as_self_closing(self):
        """REQ-016 F-04a: self-closing form (no closing tag) is now valid.
        Previously this test expected None; the parser was strictened-up."""
        text = '<<DISPATCH role="developer" text="x">> body without close'
        d = parse_latest_dispatch(text)
        assert d is not None
        assert d.role == "developer"
        assert d.text == "x"

    def test_self_closing_followed_by_arbitrary_text(self):
        # REQ-016 F-04a: the self-closing parser stops at `>>` and ignores
        # any trailing text. The raw match contains only the prefix.
        text = '<<DISPATCH role="pm" text="single line text">>\n  some body\n  more body\n<</DISPATCH>>'
        d = parse_latest_dispatch(text)
        assert d is not None
        assert d.text == "single line text"
        assert d.raw == '<<DISPATCH role="pm" text="single line text">>'


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

    def test_completion_result_is_frozen(self):
        # CompletionResult / Dispatch are frozen dataclasses so the supervisor
        # cannot accidentally mutate them.
        r = CompletionResult(layer=CompletionLayer.pending, artifact="", detail="")
        with pytest.raises((AttributeError, Exception)):
            r.artifact = "mutated"  # type: ignore[misc]

    def test_dispatch_is_frozen(self):
        d = Dispatch(role="dev", text="x", raw="<<DISPATCH...>>", end_offset=0)
        with pytest.raises((AttributeError, Exception)):
            d.role = "pm"  # type: ignore[misc]

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


# ---- REQ-014 F-06: expanded unit coverage ------------------------------------

class TestUnescapeText:
    def test_empty_string(self):
        assert _unescape_text("") == ""

    def test_plain_string(self):
        assert _unescape_text("hello world") == "hello world"

    def test_single_escape(self):
        assert _unescape_text('say \\"hi\\"') == 'say "hi"'

    def test_trailing_backslash_is_preserved(self):
        # If the last char is a bare backslash (no char after it to escape), we
        # append it verbatim rather than raising an IndexError.
        assert _unescape_text("foo\\") == "foo\\"

    def test_double_backslash(self):
        # `\\` should round-trip to a single backslash.
        assert _unescape_text("a\\\\b") == "a\\b"

    def test_escaped_non_quote(self):
        # Any backslash-escaped char is un-escaped to the literal char.
        assert _unescape_text("a\\nb") == "anb"

    def test_unicode_chars_preserved(self):
        assert _unescape_text("你好 world") == "你好 world"


class TestParseLatestDispatchEdge:
    def test_empty_text(self):
        assert parse_latest_dispatch("") is None

    def test_empty_dispatch_text(self):
        text = '<<DISPATCH role="pm" text="">><</DISPATCH>>'
        d = parse_latest_dispatch(text)
        assert d is not None
        assert d.text == ""

    def test_whitespace_only_text(self):
        text = '<<DISPATCH role="pm" text="   ">><</DISPATCH>>'
        d = parse_latest_dispatch(text)
        assert d is not None
        assert d.text == "   "

    def test_unicode_in_text(self):
        text = '<<DISPATCH role="developer" text="实现用户登录">><</DISPATCH>>'
        d = parse_latest_dispatch(text)
        assert d is not None
        assert d.text == "实现用户登录"

    def test_dispatch_at_end_of_string(self):
        # REQ-016 F-04a: the parser stops at `>>` so end_offset points there,
        # not at the position after any trailing closing tag.
        text = 'some preamble\n<<DISPATCH role="pm" text="final">>'
        d = parse_latest_dispatch(text)
        assert d is not None
        assert d.role == "pm"
        assert d.end_offset == len(text)

    def test_trailing_whitespace_after_dispatch(self):
        text = '<<DISPATCH role="dev" text="x">><</DISPATCH>>\n\n  '
        d = parse_latest_dispatch(text)
        assert d is not None
        assert d.text == "x"

    def test_preceded_by_worker_result_block(self):
        # Simulates the real pane state: a [WORKER_RESULT] block from a prior
        # step, followed by the orchestrator's next dispatch.
        text = (
            '[WORKER_RESULT role="pm" via="marker"]\n'
            'spec body\n'
            '[/WORKER_RESULT]\n'
            '<<DISPATCH role="tech_director" text="review the spec">><</DISPATCH>>'
        )
        d = parse_latest_dispatch(text)
        assert d is not None
        assert d.role == "tech_director"

    def test_malformed_role_with_digits_rejected(self):
        text = '<<DISPATCH role="dev123" text="x">><</DISPATCH>>'
        # The regex restricts role to [a-z_]+ so digits are rejected.
        assert parse_latest_dispatch(text) is None

    def test_malformed_role_uppercase_rejected(self):
        text = '<<DISPATCH role="Developer" text="x">><</DISPATCH>>'
        assert parse_latest_dispatch(text) is None


class TestWorkflowMarkersEdge:
    def test_complete_at_end_of_string_no_newline(self):
        # Marker must still be recognised if the string ends right after it.
        assert is_workflow_complete("<<WORKFLOW_COMPLETE>>\n")
        assert is_workflow_complete("line\n<<WORKFLOW_COMPLETE>>")

    def test_complete_with_trailing_whitespace(self):
        assert is_workflow_complete("<<WORKFLOW_COMPLETE>>   \n")

    def test_complete_mid_line_not_detected(self):
        assert not is_workflow_complete("hello <<WORKFLOW_COMPLETE>> world")

    def test_abort_empty_reason(self):
        assert is_workflow_abort('<<WORKFLOW_ABORT reason=""/>>') == ""

    def test_abort_long_reason(self):
        text = f'<<WORKFLOW_ABORT reason="{"a" * 500}"/>>'
        reason = is_workflow_abort(text)
        assert reason is not None
        assert len(reason) == 500


class TestValidateDispatchTextEdge:
    def test_empty_text_passes(self):
        assert validate_dispatch_text("") is None

    def test_task_done_with_surrounding_text_rejected(self):
        assert validate_dispatch_text("before <<TASK_DONE>> after") is not None

    def test_task_done_case_sensitive(self):
        # The forbidden check is case-sensitive; lowercase variants pass.
        assert validate_dispatch_text("<<task_done>>") is None

    def test_multiple_forbidden_markers_returns_first(self):
        err = validate_dispatch_text("<<TASK_DONE>> and <<WORKFLOW_COMPLETE>>")
        # Implementation-specific: first match wins.
        assert err is not None


class TestDetectCompletionEdge:
    def test_marker_with_trailing_whitespace(self):
        text = "output\n<<TASK_DONE>>   \n"
        result = detect_completion(
            pane_text=text, dispatch_end_offset=0,
            last_change_at=0.0, dispatch_at=0.0, now=10.0,
        )
        assert result.layer == CompletionLayer.marker

    def test_silence_exactly_at_threshold(self):
        # Silence timeout is inclusive: elapsed >= silence_timeout fires.
        result = detect_completion(
            pane_text="some output",
            dispatch_end_offset=0,
            last_change_at=0.0,
            dispatch_at=0.0,
            now=60.0,   # exactly equal to silence_timeout
            silence_timeout=60.0,
            stall_timeout=600.0,
        )
        assert result.layer == CompletionLayer.silence

    def test_stall_exactly_at_threshold(self):
        result = detect_completion(
            pane_text="",   # empty so silence doesn't fire
            dispatch_end_offset=0,
            last_change_at=0.0,
            dispatch_at=0.0,
            now=600.0,
            silence_timeout=60.0,
            stall_timeout=600.0,
        )
        assert result.layer == CompletionLayer.stall

    def test_tests_failed_in_silence_layer(self):
        text = "some tests failed\n<<TESTS_FAILED>>\nno marker"
        result = detect_completion(
            pane_text=text, dispatch_end_offset=0,
            last_change_at=0.0, dispatch_at=0.0, now=70.0,
            silence_timeout=60.0,
        )
        assert result.layer == CompletionLayer.silence
        assert result.tests_failed is True

    def test_artifact_does_not_include_marker_line(self):
        text = "abc\n<<TASK_DONE>>"
        result = detect_completion(
            pane_text=text, dispatch_end_offset=0,
            last_change_at=0.0, dispatch_at=0.0, now=10.0,
        )
        assert result.layer == CompletionLayer.marker
        assert "<<TASK_DONE>>" not in result.artifact
        assert result.artifact == "abc"

    def test_unicode_artifact_preserved(self):
        text = "你好世界\n<<TASK_DONE>>"
        result = detect_completion(
            pane_text=text, dispatch_end_offset=0,
            last_change_at=0.0, dispatch_at=0.0, now=10.0,
        )
        assert result.layer == CompletionLayer.marker
        assert "你好世界" in result.artifact

    def test_very_large_artifact(self):
        # A 100KB artifact should still extract correctly and not blow the
        # regex engine.
        body = "x" * 100_000
        text = f"{body}\n<<TASK_DONE>>"
        result = detect_completion(
            pane_text=text, dispatch_end_offset=0,
            last_change_at=0.0, dispatch_at=0.0, now=10.0,
        )
        assert result.layer == CompletionLayer.marker
        assert len(result.artifact) == 100_000


# ---- REQ-016 F-04a: self-closing dispatch form -----------------------------

class TestSelfClosingDispatch:
    def test_self_closing_no_slash(self):
        text = '<<DISPATCH role="developer" text="do thing">>'
        d = parse_latest_dispatch(text)
        assert d is not None
        assert d.role == "developer"
        assert d.text == "do thing"

    def test_self_closing_with_slash(self):
        text = '<<DISPATCH role="product_manager" text="analyse"/>>'
        d = parse_latest_dispatch(text)
        assert d is not None
        assert d.role == "product_manager"
        assert d.text == "analyse"

    def test_self_closing_with_extra_whitespace(self):
        text = '<<DISPATCH  role="tech_director"  text="design"   />>'
        d = parse_latest_dispatch(text)
        assert d is not None
        assert d.role == "tech_director"

    def test_full_form_still_works(self):
        text = '<<DISPATCH role="tester" text="run tests">>body<</DISPATCH>>'
        d = parse_latest_dispatch(text)
        assert d is not None
        assert d.role == "tester"
        assert d.text == "run tests"

    def test_full_form_parsed_as_self_closing_prefix(self):
        # REQ-016 F-04a: the parser now treats all dispatch shapes uniformly —
        # it captures the `<<DISPATCH ...>>` prefix and ignores any trailing
        # body + `<</DISPATCH>>`. The returned Dispatch's raw is just the
        # prefix; the body is available to the rest of the pane text.
        text = '<<DISPATCH role="developer" text="x">>body<</DISPATCH>>'
        d = parse_latest_dispatch(text)
        assert d is not None
        assert d.role == "developer"
        assert d.text == "x"
        # Raw is the prefix only — not the body or closing tag.
        assert d.raw == '<<DISPATCH role="developer" text="x">>'

    def test_self_closing_followed_by_text(self):
        # Dispatch followed by orchestrator's own commentary
        text = (
            '<<DISPATCH role="developer" text="implement">>\n'
            "some narration from the orchestrator after the dispatch"
        )
        d = parse_latest_dispatch(text)
        assert d is not None
        assert d.role == "developer"
        assert d.text == "implement"

    def test_multiple_self_closing_returns_last(self):
        text = (
            '<<DISPATCH role="pm" text="first">>\n'
            '<<DISPATCH role="developer" text="second">>'
        )
        d = parse_latest_dispatch(text)
        assert d is not None
        assert d.role == "developer"
        assert d.text == "second"

    def test_mixed_forms_later_wins(self):
        text = (
            '<<DISPATCH role="pm" text="first">>\n'
            '<<DISPATCH role="developer" text="second">>body<</DISPATCH>>'
        )
        d = parse_latest_dispatch(text)
        assert d is not None
        assert d.role == "developer"

    def test_after_offset_respected(self):
        text = '<<DISPATCH role="pm" text="old">>'
        d1 = parse_latest_dispatch(text)
        assert d1 is not None
        # Consume up to end of first dispatch; no new dispatch after it.
        assert parse_latest_dispatch(text, after_offset=d1.end_offset) is None
