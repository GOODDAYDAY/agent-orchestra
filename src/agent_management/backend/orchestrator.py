"""REQ-012 v2 — Pure functions for orchestrator dispatch parsing & completion detection.

This module is intentionally side-effect free: no IO, no asyncio, no time calls.
The supervisor injects timestamps and feeds in captured pane text. This makes
every function unit-testable without tmux, fakes, or live LLMs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# ---- Regexes -----------------------------------------------------------------

# Match: <<DISPATCH role="developer" text="...">>...<</DISPATCH>>
# - role: lowercase identifier
# - text: backslash-escaped quoted string body
# Captures the entire block so callers can compute its end offset.
_DISPATCH_RE = re.compile(
    r'<<DISPATCH\s+role="(?P<role>[a-z_]+)"\s+text="(?P<text>(?:[^"\\]|\\.)*)"\s*>>'
    r'(?P<body>.*?)'
    r'<</DISPATCH>>',
    re.DOTALL,
)

# A worker has finished when a line starting with <<TASK_DONE>> appears.
_TASK_DONE_RE = re.compile(r'^<<TASK_DONE>>\s*$', re.MULTILINE)

# Workflow control markers emitted by the orchestrator itself.
_WORKFLOW_COMPLETE_RE = re.compile(r'^<<WORKFLOW_COMPLETE>>\s*$', re.MULTILINE)
_WORKFLOW_ABORT_RE = re.compile(
    r'<<WORKFLOW_ABORT\s+reason="(?P<reason>(?:[^"\\]|\\.)*)"\s*/?>>',
)

# Tester optional secondary marker; emitted on the line above <<TASK_DONE>>
# when tests fail. Recognised by the supervisor to drive workflow loop-back.
TESTS_FAILED_MARKER = "<<TESTS_FAILED>>"
_TESTS_FAILED_RE = re.compile(r'^<<TESTS_FAILED>>\s*$', re.MULTILINE)


# ---- Public dataclasses ------------------------------------------------------

@dataclass(frozen=True)
class Dispatch:
    """A parsed dispatch block from the orchestrator's pane output."""
    role: str           # the lowercased role identifier from the role= attribute
    text: str           # the prompt body (after un-escaping \" → ")
    raw: str            # the entire dispatch block source (for logging)
    end_offset: int     # byte offset in the source string where this block ends


class CompletionLayer(str, Enum):
    """Which detection layer fired (or `pending` if none)."""
    pending = "pending"
    marker = "marker"
    silence = "silence"
    stall = "stall"
    error = "error"


@dataclass(frozen=True)
class CompletionResult:
    layer: CompletionLayer
    artifact: str       # extracted worker output (empty for pending/error/stall)
    detail: str         # human-readable annotation for logging
    tests_failed: bool = False  # set when the artifact contains <<TESTS_FAILED>>


# ---- Helpers -----------------------------------------------------------------

def _unescape_text(escaped: str) -> str:
    """Reverse the backslash-escaping in dispatch `text=` attributes."""
    out = []
    i = 0
    while i < len(escaped):
        ch = escaped[i]
        if ch == "\\" and i + 1 < len(escaped):
            out.append(escaped[i + 1])
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# ---- Public parsers ----------------------------------------------------------

def parse_latest_dispatch(
    orchestrator_pane_text: str,
    after_offset: int = 0,
) -> Optional[Dispatch]:
    """Find the *last* dispatch block whose end is after `after_offset`.

    Returns None if no new dispatch is present. The orchestrator may emit only
    one dispatch per turn but if it produces several, only the most recent
    counts (the supervisor consumes them in order anyway via offset bookkeeping).
    """
    if after_offset >= len(orchestrator_pane_text):
        return None
    last: Optional[Dispatch] = None
    for m in _DISPATCH_RE.finditer(orchestrator_pane_text):
        if m.end() <= after_offset:
            continue
        last = Dispatch(
            role=m.group("role").lower(),
            text=_unescape_text(m.group("text")),
            raw=m.group(0),
            end_offset=m.end(),
        )
    return last


def is_workflow_complete(orchestrator_pane_text: str, after_offset: int = 0) -> bool:
    """True if `<<WORKFLOW_COMPLETE>>` appears at the start of a line after offset."""
    if after_offset >= len(orchestrator_pane_text):
        return False
    return bool(_WORKFLOW_COMPLETE_RE.search(orchestrator_pane_text[after_offset:]))


def is_workflow_abort(orchestrator_pane_text: str, after_offset: int = 0) -> Optional[str]:
    """If `<<WORKFLOW_ABORT reason="...">>` appears, return the reason; else None."""
    if after_offset >= len(orchestrator_pane_text):
        return None
    m = _WORKFLOW_ABORT_RE.search(orchestrator_pane_text[after_offset:])
    return _unescape_text(m.group("reason")) if m else None


# ---- Public validation -------------------------------------------------------

_FORBIDDEN_IN_DISPATCH_TEXT = ("<<TASK_DONE>>", "<<WORKFLOW_COMPLETE>>", "<<WORKFLOW_ABORT")


def validate_dispatch_text(text: str) -> Optional[str]:
    """Return an error string if the dispatch text contains a forbidden control sequence,
    otherwise None.

    Forbidden: any of the platform-recognised completion / control markers, because
    the dispatch text is sent to a worker pane and would otherwise trip the worker's
    own completion detector or be confused for a workflow control signal.
    """
    for marker in _FORBIDDEN_IN_DISPATCH_TEXT:
        if marker in text:
            return f"dispatch text must not contain '{marker}'"
    return None


# ---- Completion detection ----------------------------------------------------

def detect_completion(
    pane_text: str,
    dispatch_end_offset: int,
    last_change_at: float,
    dispatch_at: float,
    now: float,
    silence_timeout: float = 60.0,
    stall_timeout: float = 600.0,
) -> CompletionResult:
    """Inspect the worker pane text below `dispatch_end_offset` and decide which
    completion layer fires.

    All time arguments are caller-supplied so this function is pure and
    deterministic in tests.

    Layer precedence:
        1. marker  — `<<TASK_DONE>>` line found  (primary)
        2. silence — pane has not changed for `silence_timeout` seconds  (secondary)
        3. stall   — `stall_timeout` elapsed since dispatch with no other signal (tertiary)
        4. pending — none of the above
    """
    relevant = pane_text[dispatch_end_offset:] if dispatch_end_offset > 0 else pane_text

    # 1. Marker layer (primary)
    marker_match = _TASK_DONE_RE.search(relevant)
    if marker_match:
        artifact = relevant[: marker_match.start()].rstrip()
        tests_failed = bool(_TESTS_FAILED_RE.search(artifact))
        return CompletionResult(
            layer=CompletionLayer.marker,
            artifact=artifact,
            detail=f"<<TASK_DONE>> at offset {marker_match.start()}",
            tests_failed=tests_failed,
        )

    # 2. Silence layer (secondary) — only fires if there is *some* output
    silence_elapsed = now - last_change_at
    if relevant.strip() and silence_elapsed >= silence_timeout:
        tests_failed = bool(_TESTS_FAILED_RE.search(relevant))
        return CompletionResult(
            layer=CompletionLayer.silence,
            artifact=relevant.rstrip(),
            detail=f"silence for {silence_elapsed:.0f}s (no <<TASK_DONE>>)",
            tests_failed=tests_failed,
        )

    # 3. Stall layer (tertiary)
    total_elapsed = now - dispatch_at
    if total_elapsed >= stall_timeout:
        return CompletionResult(
            layer=CompletionLayer.stall,
            artifact="",
            detail=f"stall: {total_elapsed:.0f}s elapsed since dispatch",
        )

    # 4. Still pending
    return CompletionResult(layer=CompletionLayer.pending, artifact="", detail="")
