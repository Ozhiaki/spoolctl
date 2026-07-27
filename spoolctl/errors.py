"""Parser-independent error objects for spoolctl command surfaces."""

from __future__ import annotations

from typing import Any

from spoolctl.models import EXIT_INPUT


class CliError(Exception):
    """A contract error: code + message + remediation + exit code."""

    def __init__(
        self,
        code: str,
        message: str,
        remediation: str,
        exit_code: int = EXIT_INPUT,
        did_you_mean: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.remediation = remediation
        self.exit_code = exit_code
        self.did_you_mean = did_you_mean

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
            "exit_code": self.exit_code,
        }
        if self.did_you_mean is not None:
            d["did_you_mean"] = self.did_you_mean
        return d
