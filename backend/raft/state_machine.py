"""Deterministic key-value state machine applied only to committed entries."""

from __future__ import annotations

from typing import Any

from backend.raft.models import LogEntry
from backend.raft.persistence import Storage


class KVStateMachine:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.applied_index = storage.applied_index()
        self.applied_term = storage.applied_term()

    def apply(self, entry: LogEntry) -> dict[str, Any] | None:
        if entry.index != self.applied_index + 1:
            raise RuntimeError(
                f"non-contiguous apply: expected {self.applied_index + 1}, got {entry.index}"
            )
        result = self.storage.apply_entry(entry)
        self.applied_index = entry.index
        self.applied_term = entry.term
        return result

    def get(self, key: str) -> tuple[str | None, int]:
        return self.storage.get_kv(key)

    def list(self) -> dict[str, str]:
        return self.storage.list_kv()

    def count(self) -> int:
        return self.storage.kv_count()

    def snapshot(self) -> tuple[dict[str, str], list[dict[str, Any]]]:
        return self.storage.list_kv(), self.storage.applied_history()
