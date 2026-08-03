"""The tool response contract.

Every tool returns one of four states. Empty results are NEVER collapsed into a
bare empty list: with an account only days old, "no data" is a common and
legitimate answer, and it must be distinguishable from "broken".

`no_data` and `warming_up` are not errors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ResultState(str, Enum):
    OK = "ok"
    NO_DATA = "no_data"
    WARMING_UP = "warming_up"
    ERROR = "error"


@dataclass
class ToolResult:
    state: ResultState
    data: Any | None = None
    message: str | None = None
    remedy: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, data: Any, **meta: Any) -> "ToolResult":
        return cls(state=ResultState.OK, data=data, meta=meta)

    @classmethod
    def no_data(cls, message: str, **meta: Any) -> "ToolResult":
        return cls(state=ResultState.NO_DATA, message=message, meta=meta)

    @classmethod
    def warming_up(cls, message: str, **meta: Any) -> "ToolResult":
        return cls(state=ResultState.WARMING_UP, message=message, meta=meta)

    @classmethod
    def error(cls, message: str, remedy: str | None = None, **meta: Any) -> "ToolResult":
        return cls(state=ResultState.ERROR, message=message, remedy=remedy, meta=meta)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"state": self.state.value}
        if self.data is not None:
            payload["data"] = self.data
        if self.message is not None:
            payload["message"] = self.message
        if self.remedy is not None:
            payload["remedy"] = self.remedy
        payload.update(self.meta)
        return payload
