"""Log entry and RPC wire models shared by nodes and tools."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field


EntryKind = Literal["command", "noop"]


@dataclass(frozen=True)
class LogEntry:
    index: int
    term: int
    kind: EntryKind
    command: dict[str, Any] | None = None

    def command_json(self) -> str:
        if self.command is None:
            return "null"
        return json.dumps(self.command, sort_keys=True, separators=(",", ":"))

    def command_hash(self) -> str:
        payload = f"{self.kind}:{self.command_json()}".encode()
        return hashlib.sha256(payload).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "term": self.term,
            "kind": self.kind,
            "command": self.command,
        }

    @classmethod
    def from_row(cls, index: int, term: int, kind: str, command_json: str | None) -> "LogEntry":
        command = json.loads(command_json) if command_json else None
        return cls(index=index, term=term, kind=kind, command=command)  # type: ignore[arg-type]


class RequestVoteRequest(BaseModel):
    term: int = Field(ge=0)
    candidate_id: str
    last_log_index: int = Field(ge=0)
    last_log_term: int = Field(ge=0)


class RequestVoteResponse(BaseModel):
    term: int
    vote_granted: bool
    voter_id: str


class AppendEntriesRequest(BaseModel):
    term: int = Field(ge=0)
    leader_id: str
    prev_log_index: int = Field(ge=0)
    prev_log_term: int = Field(ge=0)
    entries: list[dict[str, Any]] = Field(default_factory=list)
    leader_commit: int = Field(ge=0)


class AppendEntriesResponse(BaseModel):
    term: int
    success: bool
    match_index: int = 0
    last_index: int = 0
    conflict_index: int = 0
    responder_id: str = ""


class InstallSnapshotRequest(BaseModel):
    term: int = Field(ge=0)
    leader_id: str
    last_included_index: int = Field(ge=0)
    last_included_term: int = Field(ge=0)
    data: dict[str, Any]


class InstallSnapshotResponse(BaseModel):
    term: int
    success: bool
    match_index: int = 0
    responder_id: str = ""
