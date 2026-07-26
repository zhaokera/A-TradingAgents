"""Shared structured errors for the governed decision workflow."""

from __future__ import annotations

from typing import Any, Mapping, Optional


class DecisionWorkflowError(RuntimeError):
    """An expected workflow failure that can cross the API/CLI boundary."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.status_code = int(status_code)
        self.details = dict(details or {})
