"""The marker for a tool result that reports a failure.

Handlers return refusals and failures as text so the caller sees the reason.
Wrapping that text in `ErrorText` lets call_tool send it with isError set, so a
client can tell it from output - a log line or a field read by `path` can
itself start with "Error:".
"""

from __future__ import annotations


class ErrorText(str):
    """Text of a failed tool call; call_tool sends it with isError set."""
