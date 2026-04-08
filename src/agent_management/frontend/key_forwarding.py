"""REQ-015 — Pure-function Textual key → tmux send-keys argv mapping.

Used by the dedicated input-box focus catcher to forward every Textual
key event to a tmux pane via `SessionManager.send_raw_keys`. The function
is intentionally side-effect free and exception-free so it is trivial to
unit-test and to extend with new keys.
"""
from __future__ import annotations

from typing import Optional

# ---- Special key table -------------------------------------------------------

# Each entry maps a Textual events.Key.key string to the tmux send-keys argv
# tokens that produce the corresponding terminal input. tmux send-keys accepts
# named keys ("Enter", "Up", "C-c", ...) so we never have to worry about raw
# escape sequences.
_SPECIAL: dict[str, list[str]] = {
    "enter":     ["Enter"],
    "tab":       ["Tab"],
    "backspace": ["BSpace"],
    "delete":    ["DC"],
    "escape":    ["Escape"],
    "up":        ["Up"],
    "down":      ["Down"],
    "left":      ["Left"],
    "right":     ["Right"],
    "home":      ["Home"],
    "end":       ["End"],
    "pageup":    ["PPage"],
    "pagedown":  ["NPage"],
    "space":     ["Space"],
    "insert":    ["IC"],
}

# F1..F12
for _i in range(1, 13):
    _SPECIAL[f"f{_i}"] = [f"F{_i}"]
del _i


# ---- Public API --------------------------------------------------------------

def tmux_args_for_key(event) -> Optional[list[str]]:
    """Resolve a Textual events.Key into a tmux send-keys argv list.

    REQ-016 F-02 — the previous implementation only looked at `event.key`.
    Textual reports shift-modified punctuation like `!@#$%^&*()` via
    `event.key="exclamation_mark"` etc. while `event.character` holds the
    actual typed character "!". Dropping those led to silent failures where
    punctuation never reached the agent.

    Priority order:
      1. Named special keys and ctrl combinations → use `event.key` because
         it gives precise tmux key names (Enter, Tab, Up, C-c, ...).
      2. Printable typed characters → prefer `event.character`.
      3. Fallback → `event.key` (older Textual versions that only set key).

    Returns None (never raises) for anything we can't resolve; callers drop
    the keystroke silently.
    """
    key_name: str = getattr(event, "key", "") or ""

    # 1. Named specials and ctrl combos — event.key is authoritative.
    if key_name in _SPECIAL or key_name.startswith("ctrl+"):
        return textual_to_tmux(key_name)

    # 2. Printable character input — prefer event.character.
    character = getattr(event, "character", None)
    if character is not None and character != "" and character.isprintable():
        return textual_to_tmux(character)

    # 3. Fallback — older Textual versions may only set event.key.
    if key_name:
        return textual_to_tmux(key_name)

    return None


def textual_to_tmux(event_key: str) -> Optional[list[str]]:
    """Map a Textual `events.Key.key` string to a tmux send-keys argv list.

    Returns None for unrecognised or empty input. Callers (the InputBox focus
    catcher) treat None as "drop the keystroke silently". This contract makes
    the InputBox safe against future Textual versions that introduce new
    key names — unknown keys are dropped, never crash.
    """
    if not event_key:
        return None

    # 1. Named special key (Enter, Tab, arrows, function keys, ...)
    if event_key in _SPECIAL:
        return list(_SPECIAL[event_key])  # copy so callers can't mutate

    # 2. Modifier-prefixed combinations
    if event_key.startswith("ctrl+"):
        suffix = event_key[len("ctrl+"):]
        if not suffix:
            return None
        if suffix == "space":
            return ["C-Space"]
        if suffix == "]":
            return ["C-]"]
        if len(suffix) == 1:
            # tmux uses lowercase: ctrl+A and ctrl+a both map to C-a
            return [f"C-{suffix.lower()}"]
        # Multi-char ctrl combinations are not in scope (e.g. ctrl+enter)
        return None

    # 3. Single printable character — pass through verbatim. tmux send-keys
    # accepts UTF-8 characters as positional arguments and types them as if
    # the user had pressed those keys.
    if len(event_key) == 1:
        if event_key.isprintable():
            return [event_key]
        return None

    # 4. Multi-character non-ASCII string — almost certainly IME composition
    # output (e.g. Chinese / Japanese typed via an IME). Pass through.
    # Multi-character ASCII strings are almost always Textual key names we
    # don't recognise (e.g. "super+meta+x", "ctrl", " ", "shift+enter") and
    # are dropped to avoid typing them literally into the agent.
    if event_key.isprintable() and not event_key.isascii():
        return [event_key]

    # 5. Anything else → drop.
    return None
