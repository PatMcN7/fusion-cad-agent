"""Exception types with stable identity.

This module is deliberately excluded from the hot-reload set. Reloading a
module rebinds its classes, so an exception raised by the freshly reloaded
`assertions` is a *different class object* than the one the shell imported at
startup, and `isinstance` silently stops matching. Keeping the exception
hierarchy in a module that is never reloaded makes identity stable, so the
shell can catch one base class instead of comparing class names as strings.
"""

from __future__ import annotations

from typing import Any


class BridgeError(Exception):
    """Base for everything the bridge reports as a structured error."""

    code = "internal_error"

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.context = context


class ProtocolError(BridgeError):
    """The request does not match the protocol."""

    code = "bad_request"


class ArgumentError(BridgeError):
    """A tool argument is missing, malformed, or out of range."""

    code = "invalid_arguments"


class PatternError(ArgumentError):
    """Parameters cannot produce a valid pattern."""


class SelectionError(BridgeError):
    """A geometry query did not resolve to what the caller required."""

    code = "tool_failure"


class OpError(BridgeError):
    """A modelling primitive was asked for something it cannot do."""

    code = "tool_failure"


class ToolFailure(BridgeError):
    """The kernel or the model is not in a state the tool can work with."""

    code = "tool_failure"


class CheckFailed(BridgeError):
    """One or more required checks failed."""

    code = "assertion_failed"

    def __init__(self, checks, label: str = "", **context: Any) -> None:
        self.checks = list(checks)
        self.label = label
        prefix = f"{label}: " if label else ""
        super().__init__(
            prefix + "; ".join(c.describe() for c in self.checks), **context
        )

    @property
    def failed_checks(self) -> list[dict[str, Any]]:
        return [c.as_dict() for c in self.checks]
