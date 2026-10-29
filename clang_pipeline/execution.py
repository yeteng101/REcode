from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


class AnalysisError(RuntimeError):
    """Base class for errors that can be exposed by the Agent API."""

    code = "analysis_error"

    def __init__(
        self,
        message: str,
        *,
        stage: str | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.retryable = retryable
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": type(self).__name__,
            "code": self.code,
            "message": str(self),
            "stage": self.stage,
            "retryable": self.retryable,
            "details": self.details,
        }


class RepositoryNotFoundError(AnalysisError):
    code = "repository_not_found"


class RepositoryCheckoutError(AnalysisError):
    code = "repository_checkout_failed"


class RepositoryTooLargeError(AnalysisError):
    code = "repository_too_large"


class BuildConfigurationError(AnalysisError):
    code = "build_configuration_failed"


class PipelineStageError(AnalysisError):
    code = "pipeline_stage_failed"


class ClangStageError(PipelineStageError):
    code = "clang_stage_failed"


class AnalysisCancelled(AnalysisError):
    code = "analysis_cancelled"


class AnalysisTimedOut(AnalysisError):
    code = "analysis_timeout"


@dataclass
class CancellationToken:
    """Thread-safe cooperative cancellation signal."""

    _event: threading.Event = field(default_factory=threading.Event)
    reason: str = ""
    kind: str = "cancelled"

    def cancel(self, reason: str = "cancelled by caller", *, kind: str = "cancelled") -> None:
        if not self._event.is_set():
            self.reason = reason
            self.kind = kind
            self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def check(self, stage: str = "") -> None:
        if not self._event.is_set():
            return
        if self.kind == "timeout":
            raise AnalysisTimedOut(
                self.reason or "analysis timed out",
                stage=stage or None,
                retryable=True,
            )
        raise AnalysisCancelled(
            self.reason or "analysis cancelled",
            stage=stage or None,
            retryable=False,
        )
