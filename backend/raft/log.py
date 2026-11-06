"""In-memory Raft log with a snapshot base.

The log is append-only during normal operation. Followers truncate conflicting suffixes
after a failed consistency check; leaders never truncate their own entries.
"""

from __future__ import annotations

from backend.raft.models import LogEntry


class RaftLog:
    def __init__(
        self,
        entries: list[LogEntry] | None = None,
        snapshot_index: int = 0,
        snapshot_term: int = 0,
    ) -> None:
        self._entries: list[LogEntry] = sorted(entries or [], key=lambda e: e.index)
        self.snapshot_index = snapshot_index
        self.snapshot_term = snapshot_term
        self._validate_contiguous()

    def _validate_contiguous(self) -> None:
        for offset, entry in enumerate(self._entries):
            expected = self.snapshot_index + offset + 1
            if entry.index != expected:
                raise ValueError(
                    f"log entries are not contiguous: expected index {expected}, got {entry.index}"
                )

    @property
    def entries(self) -> list[LogEntry]:
        return self._entries

    @property
    def last_index(self) -> int:
        if self._entries:
            return self._entries[-1].index
        return self.snapshot_index

    @property
    def last_term(self) -> int:
        if self._entries:
            return self._entries[-1].term
        return self.snapshot_term

    @property
    def length(self) -> int:
        return max(0, self.last_index - self.snapshot_index)

    def term_at(self, index: int) -> int | None:
        if index == self.snapshot_index:
            return self.snapshot_term
        if index < self.snapshot_index or index > self.last_index:
            return None
        return self._entries[index - self.snapshot_index - 1].term

    def entry_at(self, index: int) -> LogEntry | None:
        if index <= self.snapshot_index or index > self.last_index:
            return None
        return self._entries[index - self.snapshot_index - 1]

    def entries_from(self, index: int, limit: int | None = None) -> list[LogEntry]:
        start = max(index, self.snapshot_index + 1)
        if start > self.last_index:
            return []
        offset = start - self.snapshot_index - 1
        end = len(self._entries) if limit is None else offset + max(0, limit)
        return self._entries[offset:end]

    def append(self, entry: LogEntry) -> None:
        if entry.index != self.last_index + 1:
            raise ValueError(f"cannot append index {entry.index}; last index is {self.last_index}")
        if entry.index <= self.snapshot_index:
            raise ValueError(f"cannot append index {entry.index} at or below snapshot")
        self._entries.append(entry)

    def truncate_from(self, index: int) -> list[LogEntry]:
        """Remove entries with index >= ``index`` (conflict resolution)."""
        if index <= self.snapshot_index:
            raise ValueError("cannot truncate into compacted history")
        removed = [e for e in self._entries if e.index >= index]
        self._entries = [e for e in self._entries if e.index < index]
        return removed

    def compact_to(self, index: int, term: int) -> None:
        """Drop entries through ``index`` and set the snapshot base."""
        if index < self.snapshot_index:
            return
        if index > self.last_index:
            raise ValueError("cannot compact beyond the last log index")
        self._entries = [e for e in self._entries if e.index > index]
        self.snapshot_index = index
        self.snapshot_term = term

    def has_entry(self, index: int, term: int) -> bool:
        return self.term_at(index) == term

    def matches(self, other: "RaftLog") -> bool:
        """Full-log equality below the smaller snapshot boundary is not knowable."""
        for entry in self._entries:
            other_entry = other.entry_at(entry.index)
            if other_entry is None or other_entry.term != entry.term:
                return False
        return True
