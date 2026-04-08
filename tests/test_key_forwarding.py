"""REQ-015 — Unit tests for the Textual → tmux key mapping."""
from __future__ import annotations

import string

import pytest

from agent_management.frontend.key_forwarding import (
    textual_to_tmux,
    tmux_args_for_key,
)


# ---- Special keys ------------------------------------------------------------

class TestSpecialKeys:
    @pytest.mark.parametrize("key,expected", [
        ("enter",     ["Enter"]),
        ("tab",       ["Tab"]),
        ("backspace", ["BSpace"]),
        ("delete",    ["DC"]),
        ("escape",    ["Escape"]),
        ("up",        ["Up"]),
        ("down",      ["Down"]),
        ("left",      ["Left"]),
        ("right",     ["Right"]),
        ("home",      ["Home"]),
        ("end",       ["End"]),
        ("pageup",    ["PPage"]),
        ("pagedown",  ["NPage"]),
        ("space",     ["Space"]),
        ("insert",    ["IC"]),
    ])
    def test_named_special(self, key, expected):
        assert textual_to_tmux(key) == expected

    @pytest.mark.parametrize("i", range(1, 13))
    def test_function_keys(self, i):
        assert textual_to_tmux(f"f{i}") == [f"F{i}"]


# ---- Ctrl combinations -------------------------------------------------------

class TestCtrlKeys:
    @pytest.mark.parametrize("letter", list("abcdefghijklmnopqrstuvwxyz"))
    def test_ctrl_lowercase_letter(self, letter):
        assert textual_to_tmux(f"ctrl+{letter}") == [f"C-{letter}"]

    def test_ctrl_uppercase_normalised(self):
        # Textual uses lowercase but be defensive: ctrl+A → C-a
        assert textual_to_tmux("ctrl+A") == ["C-a"]

    def test_ctrl_space(self):
        assert textual_to_tmux("ctrl+space") == ["C-Space"]

    def test_ctrl_close_bracket(self):
        assert textual_to_tmux("ctrl+]") == ["C-]"]

    def test_ctrl_with_long_suffix_returns_none(self):
        # Multi-char ctrl combinations are not in scope
        assert textual_to_tmux("ctrl+enter") is None

    def test_ctrl_with_empty_suffix(self):
        assert textual_to_tmux("ctrl+") is None


# ---- Printable characters ----------------------------------------------------

class TestPrintableChars:
    @pytest.mark.parametrize("ch", list(string.ascii_lowercase))
    def test_lowercase_letters(self, ch):
        assert textual_to_tmux(ch) == [ch]

    @pytest.mark.parametrize("ch", list(string.ascii_uppercase))
    def test_uppercase_letters(self, ch):
        assert textual_to_tmux(ch) == [ch]

    @pytest.mark.parametrize("ch", list(string.digits))
    def test_digits(self, ch):
        assert textual_to_tmux(ch) == [ch]

    @pytest.mark.parametrize("ch", list("!@#$%^&*()-_=+[]{};:,.<>/?\\|`~"))
    def test_punctuation(self, ch):
        assert textual_to_tmux(ch) == [ch]

    def test_double_quote(self):
        assert textual_to_tmux('"') == ['"']

    def test_single_quote(self):
        assert textual_to_tmux("'") == ["'"]


# ---- Unicode -----------------------------------------------------------------

class TestUnicode:
    def test_chinese_character(self):
        assert textual_to_tmux("你") == ["你"]

    def test_emoji_single_codepoint(self):
        # Some emojis are single Unicode chars (e.g. heart ♥)
        assert textual_to_tmux("♥") == ["♥"]

    def test_multichar_unicode_passthrough(self):
        # IME composition output may produce multi-char sequences
        result = textual_to_tmux("你好")
        assert result == ["你好"]


# ---- Garbage / unrecognised -------------------------------------------------

class TestGarbage:
    def test_empty_string(self):
        assert textual_to_tmux("") is None

    def test_whitespace_only(self):
        # Whitespace strings (other than the named "space") are not in
        # _SPECIAL and are dropped (they're modifier-only or weird events).
        # A literal space character is "space" in Textual's key naming, not " ".
        assert textual_to_tmux("   ") is None

    def test_unknown_named_key(self):
        assert textual_to_tmux("super+meta+chord") is None

    def test_modifier_only(self):
        # Pressing just Ctrl with no letter — Textual emits "ctrl" sometimes
        assert textual_to_tmux("ctrl") is None

    def test_returned_list_is_a_copy(self):
        # The internal _SPECIAL table must not be mutated by callers.
        result = textual_to_tmux("enter")
        result.append("Tampered")
        again = textual_to_tmux("enter")
        assert again == ["Enter"]


# ---- API contract -----------------------------------------------------------

# ---- REQ-016 F-02: tmux_args_for_key (event-aware) ------------------------

from types import SimpleNamespace


def _key(key: str, character=None):
    """Build a fake Textual events.Key-compatible stub."""
    return SimpleNamespace(key=key, character=character)


class TestTmuxArgsForKey:
    def test_named_special_enter(self):
        assert tmux_args_for_key(_key("enter", None)) == ["Enter"]

    def test_named_special_tab(self):
        assert tmux_args_for_key(_key("tab", None)) == ["Tab"]

    def test_ctrl_c_via_key(self):
        assert tmux_args_for_key(_key("ctrl+c", None)) == ["C-c"]

    def test_letter_via_key_only(self):
        # event.character unset — fall back to event.key
        assert tmux_args_for_key(_key("a", None)) == ["a"]

    def test_letter_via_character_priority(self):
        # Newer Textual sets character for printable keys
        assert tmux_args_for_key(_key("a", "a")) == ["a"]

    def test_shift_exclamation_via_character(self):
        # THIS is the REQ-016 F-02 regression guard: Textual reports the
        # shifted key as key="exclamation_mark" but character="!". The fix
        # is to prefer character for printable input.
        assert tmux_args_for_key(_key("exclamation_mark", "!")) == ["!"]

    def test_shift_digits_punctuation_via_character(self):
        shifts = {
            "exclamation_mark": "!",
            "at": "@",
            "hash": "#",
            "dollar_sign": "$",
            "percent_sign": "%",
            "caret": "^",
            "ampersand": "&",
            "asterisk": "*",
            "left_parenthesis": "(",
            "right_parenthesis": ")",
        }
        for key_name, character in shifts.items():
            assert tmux_args_for_key(_key(key_name, character)) == [character], (
                f"failed for key={key_name} char={character}"
            )

    def test_bracket_family_via_character(self):
        for ch in "[]{}":
            assert tmux_args_for_key(_key(f"named_{ch}", ch)) == [ch]

    def test_punctuation_family_via_character(self):
        for ch in ";:'\"<>,./?\\|`~-_=+":
            assert tmux_args_for_key(_key(f"named_{ch}", ch)) == [ch]

    def test_unicode_via_character(self):
        # IME composition: key name is synthetic, character carries the real text
        assert tmux_args_for_key(_key("ime", "你")) == ["你"]

    def test_multi_char_unicode_via_character(self):
        assert tmux_args_for_key(_key("ime", "你好")) == ["你好"]

    def test_character_none_falls_back_to_key(self):
        # Older Textual may not populate event.character
        assert tmux_args_for_key(_key("a", None)) == ["a"]

    def test_empty_event(self):
        assert tmux_args_for_key(_key("", None)) is None

    def test_unrecognised_garbage(self):
        assert tmux_args_for_key(_key("super+meta+chord", None)) is None

    def test_named_special_wins_over_character(self):
        # Even if Textual sets event.character for a named special, we keep
        # using event.key because it gives the precise tmux spec.
        assert tmux_args_for_key(_key("enter", "\n")) == ["Enter"]

    def test_ctrl_combination_wins_over_character(self):
        assert tmux_args_for_key(_key("ctrl+a", "a")) == ["C-a"]


class TestApiContract:
    def test_never_raises_on_garbage(self):
        garbage = [
            None and "x", "", "\x00", "\x1b", "ctrl++", "shift+",
            "alt+ctrl+meta+x", "1+2", "ctrl+ctrl+a",
        ]
        for item in garbage:
            # Must not raise — None or list, never an exception
            try:
                _ = textual_to_tmux(item if item is not None else "")
            except Exception as exc:
                pytest.fail(f"textual_to_tmux raised on {item!r}: {exc}")
