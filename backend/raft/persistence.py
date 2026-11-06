"""Durable node state: Raft metadata, log, KV state machine, snapshots.

SQLite is used with WAL journaling and ``synchronous=FULL`` so term/vote/log mutations are
fsynced before a node acknowledges an RPC. Each mutation is a single transaction, which
keeps "applied index" and the materialized KV state consistent across crashes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from backend.raft.models import LogEntry

HISTORY_LIMIT = 2000


class Storage:
    def __init__(self, data_dir: str, node_id: str) -> None:
        self.data_dir = data_dir
        self.node_id = node_id
        self.node_dir = os.path.join(data_dir)
        os.makedirs(self.node_dir, exist_ok=True)
        self.db_path = os.path.join(self.node_dir, "raft.db")
        self.snapshot_path = os.path.join(self.node_dir, "snapshot.json")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path, check_same_thread=False, isolation_level=None, timeout=10.0
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS log (
                    idx INTEGER PRIMARY KEY,
                    term INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    command_json TEXT
                );
                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    last_index INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS apply_history (
                    idx INTEGER PRIMARY KEY,
                    term INTEGER NOT NULL,
                    command_hash TEXT NOT NULL
                );
                """
            )
            defaults = {
                "node_id": self.node_id,
                "current_term": "0",
                "voted_for": "",
                "applied_index": "0",
                "applied_term": "0",
                "snapshot_index": "0",
                "snapshot_term": "0",
            }
            for key, value in defaults.items():
                self._conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (key, value)
                )

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------ metadata
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def get_meta_int(self, key: str, default: int = 0) -> int:
        value = self.get_meta(key)
        return int(value) if value not in (None, "") else default

    def current_term(self) -> int:
        return self.get_meta_int("current_term")

    def voted_for(self) -> str | None:
        value = self.get_meta("voted_for", "")
        return value or None

    def applied_index(self) -> int:
        return self.get_meta_int("applied_index")

    def applied_term(self) -> int:
        return self.get_meta_int("applied_term")

    def snapshot_index(self) -> int:
        return self.get_meta_int("snapshot_index")

    def snapshot_term(self) -> int:
        return self.get_meta_int("snapshot_term")

    def save_term_vote(self, term: int, voted_for: str | None) -> None:
        """Atomically persist term and vote before any RPC response."""
        with self._tx() as conn:
            conn.execute("UPDATE meta SET value=? WHERE key='current_term'", (str(term),))
            conn.execute("UPDATE meta SET value=? WHERE key='voted_for'", (voted_for or "",))

    def save_term(self, term: int) -> None:
        self.save_term_vote(term, self.voted_for())

    # ----------------------------------------------------------------------- log
    def load_entries(self) -> list[LogEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT idx, term, kind, command_json FROM log ORDER BY idx ASC"
            ).fetchall()
        return [
            LogEntry.from_row(row["idx"], row["term"], row["kind"], row["command_json"])
            for row in rows
        ]

    def append_entry(self, entry: LogEntry) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO log(idx, term, kind, command_json) VALUES (?,?,?,?)",
                (entry.index, entry.term, entry.kind, entry.command_json()
                 if entry.command is not None else None),
            )

    def append_entries(self, entries: list[LogEntry]) -> None:
        if not entries:
            return
        with self._tx() as conn:
            conn.executemany(
                "INSERT INTO log(idx, term, kind, command_json) VALUES (?,?,?,?)",
                [
                    (
                        e.index,
                        e.term,
                        e.kind,
                        e.command_json() if e.command is not None else None,
                    )
                    for e in entries
                ],
            )

    def truncate_from(self, index: int) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM log WHERE idx >= ?", (index,))

    def delete_log_through(self, index: int) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM log WHERE idx <= ?", (index,))

    def delete_all_log(self) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM log")

    def log_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM log").fetchone()
        return int(row["c"])

    # --------------------------------------------------------------- state machine
    def apply_entry(self, entry: LogEntry) -> dict[str, Any] | None:
        """Apply one committed entry and advance the applied index atomically."""
        result: dict[str, Any] | None = None
        with self._tx() as conn:
            if entry.kind == "command" and entry.command:
                op = entry.command.get("op")
                key = entry.command.get("key")
                if op == "set":
                    value = str(entry.command.get("value", ""))
                    conn.execute(
                        "INSERT INTO kv(key, value, last_index) VALUES (?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                        "last_index=excluded.last_index",
                        (key, value, entry.index),
                    )
                    result = {"op": "set", "key": key, "value": value}
                elif op == "delete":
                    conn.execute("DELETE FROM kv WHERE key=?", (key,))
                    result = {"op": "delete", "key": key}
                elif op == "noop":
                    result = None
                else:
                    raise ValueError(f"unknown command op in log: {op!r}")
            conn.execute(
                "UPDATE meta SET value=? WHERE key='applied_index'", (str(entry.index),)
            )
            conn.execute(
                "UPDATE meta SET value=? WHERE key='applied_term'", (str(entry.term),)
            )
            conn.execute(
                "INSERT OR REPLACE INTO apply_history(idx, term, command_hash) VALUES (?,?,?)",
                (entry.index, entry.term, entry.command_hash()),
            )
            conn.execute("DELETE FROM apply_history WHERE idx <= ?", (entry.index - HISTORY_LIMIT,))
        return result

    def get_kv(self, key: str) -> tuple[str | None, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT value, last_index FROM kv WHERE key=?", (key,)
            ).fetchone()
        return (row["value"], row["last_index"]) if row else (None, 0)

    def list_kv(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM kv ORDER BY key").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def kv_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM kv").fetchone()
        return int(row["c"])

    def applied_history(self, limit: int = 2000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT idx, term, command_hash FROM apply_history ORDER BY idx DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows][::-1]

    # ------------------------------------------------------------------ snapshots
    def _write_snapshot_file(self, payload: dict[str, Any]) -> None:
        # Local snapshot creation and leader InstallSnapshot can race on the same .tmp
        # file. Serialize the whole write/replace sequence so one cannot truncate the
        # other's partially written JSON.
        with self._lock:
            tmp = self.snapshot_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.snapshot_path)
            dir_fd = os.open(self.node_dir, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    def validate_snapshot_payload(self, payload: Any) -> tuple[int, int]:
        """Validate snapshot structure before it is persisted or installed.

        A snapshot is the only place where a node replaces its entire materialized state.
        Rejecting malformed metadata here prevents a partial or internally inconsistent
        snapshot from advancing ``applied_index`` past the KV state that actually exists.
        """
        if not isinstance(payload, dict):
            raise ValueError("snapshot payload must be an object")
        index = payload.get("last_included_index")
        term = payload.get("last_included_term")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index <= 0
            or not isinstance(term, int)
            or isinstance(term, bool)
            or term <= 0
        ):
            raise ValueError("snapshot index/term are invalid")
        kv = payload.get("kv", {})
        if not isinstance(kv, dict):
            raise ValueError("snapshot kv must be an object")
        for key, value in kv.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("snapshot kv keys and values must be strings")
        history = payload.get("history", [])
        if not isinstance(history, list):
            raise ValueError("snapshot history must be a list")
        previous = 0
        for item in history:
            if not isinstance(item, dict):
                raise ValueError("snapshot history entries must be objects")
            h_idx = item.get("idx")
            h_term = item.get("term")
            h_hash = item.get("command_hash")
            if (
                not isinstance(h_idx, int)
                or isinstance(h_idx, bool)
                or h_idx <= previous
                or h_idx > index
                or not isinstance(h_term, int)
                or isinstance(h_term, bool)
                or h_term <= 0
                or not isinstance(h_hash, str)
                or not h_hash
            ):
                raise ValueError("snapshot history entry is invalid")
            previous = h_idx
        return index, term

    def create_snapshot(
        self,
        last_included_index: int,
        last_included_term: int,
        kv: dict[str, str],
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = {
            "last_included_index": last_included_index,
            "last_included_term": last_included_term,
            "kv": kv,
            "history": history,
        }
        self.validate_snapshot_payload(payload)
        self._write_snapshot_file(payload)
        with self._tx() as conn:
            conn.execute(
                "UPDATE meta SET value=? WHERE key='snapshot_index'",
                (str(last_included_index),),
            )
            conn.execute(
                "UPDATE meta SET value=? WHERE key='snapshot_term'",
                (str(last_included_term),),
            )
            conn.execute("DELETE FROM log WHERE idx <= ?", (last_included_index,))
        return payload

    def load_snapshot_file(self) -> dict[str, Any] | None:
        with self._lock:
            if not os.path.exists(self.snapshot_path):
                return None
            try:
                with open(self.snapshot_path, encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (json.JSONDecodeError, OSError) as exc:
                raise RuntimeError(f"snapshot file {self.snapshot_path} is unreadable") from exc
            self.validate_snapshot_payload(payload)
            return payload

    def _install_snapshot_db(self, payload: dict[str, Any]) -> None:
        index, term = self.validate_snapshot_payload(payload)
        with self._tx() as conn:
            conn.execute("DELETE FROM kv")
            conn.executemany(
                "INSERT INTO kv(key, value, last_index) VALUES (?,?,?)",
                [(key, value, index) for key, value in payload.get("kv", {}).items()],
            )
            conn.execute("DELETE FROM apply_history")
            conn.executemany(
                "INSERT OR REPLACE INTO apply_history(idx, term, command_hash) VALUES (?,?,?)",
                [
                    (int(item["idx"]), int(item["term"]), str(item["command_hash"]))
                    for item in payload.get("history", [])
                ],
            )
            conn.execute("DELETE FROM log")
            conn.execute("UPDATE meta SET value=? WHERE key='snapshot_index'", (str(index),))
            conn.execute("UPDATE meta SET value=? WHERE key='snapshot_term'", (str(term),))
            conn.execute("UPDATE meta SET value=? WHERE key='applied_index'", (str(index),))
            conn.execute("UPDATE meta SET value=? WHERE key='applied_term'", (str(term),))

    def install_snapshot(self, payload: dict[str, Any]) -> None:
        """Install a leader snapshot and discard the local log.

        Discarding the whole log is conservative: the leader will resend any entries after
        the snapshot boundary. The file is durable before the database changes, and startup
        reconciliation recognizes a file newer than ``applied_index`` as an interrupted
        install. That is why the file is written first: the recovery path can finish the
        database half of the install without ever advancing metadata over stale KV state.
        """
        self.validate_snapshot_payload(payload)
        self._write_snapshot_file(payload)
        self._install_snapshot_db(payload)

    def _rebuild_snapshot_at_applied(self) -> None:
        index = self.applied_index()
        if index <= 0:
            return
        term = self.applied_term()
        payload = {
            "last_included_index": index,
            "last_included_term": term,
            "kv": self.list_kv(),
            "history": self.applied_history(HISTORY_LIMIT),
        }
        self.validate_snapshot_payload(payload)
        self._write_snapshot_file(payload)
        with self._tx() as conn:
            conn.execute("UPDATE meta SET value=? WHERE key='snapshot_index'", (str(index),))
            conn.execute("UPDATE meta SET value=? WHERE key='snapshot_term'", (str(term),))
            conn.execute("DELETE FROM log WHERE idx <= ?", (index,))

    def reconcile_snapshot_on_startup(self) -> tuple[int, int]:
        """Make the durable snapshot file and ``applied_index`` tell the same story.

        There are two crash windows to heal:

        * a local snapshot file was written, then the process died before log compaction;
        * an installed snapshot file was written, then the process died before the database
          transaction that replaces KV/history/applied metadata.

        The second case is the dangerous one: advancing ``applied_index`` without restoring
        the snapshot's KV data would permanently drive the state machine ahead of its actual
        contents. A *valid* file with an index above ``applied_index`` is therefore installed
        transactionally, not merely acknowledged as the new snapshot boundary.
        """
        applied = self.applied_index()
        db_index = self.snapshot_index()
        db_term = self.snapshot_term()
        if applied < db_index:
            raise RuntimeError(
                f"inconsistent durable state: applied_index={applied} < snapshot_index={db_index}"
            )
        try:
            snap = self.load_snapshot_file()
        except (RuntimeError, ValueError, OSError):
            # The database is a consistent prefix of the applied state. If a snapshot had
            # already been compacted, regenerate it from the database. If not, the log is
            # still intact and the corrupt/unfinished file can simply be replaced.
            if db_index > 0 or applied > 0:
                if db_index > 0:
                    self._rebuild_snapshot_at_applied()
                    return self.snapshot_index(), self.snapshot_term()
            try:
                if os.path.exists(self.snapshot_path):
                    os.unlink(self.snapshot_path)
            except OSError:
                pass
            return db_index, db_term
        if snap is None:
            if db_index > 0:
                self._rebuild_snapshot_at_applied()
                return self.snapshot_index(), self.snapshot_term()
            return db_index, db_term
        file_index, file_term = self.validate_snapshot_payload(snap)
        if file_index > applied:
            # Interrupted InstallSnapshot: the database has not applied the snapshot yet.
            # Restore the complete snapshot payload atomically, then report its boundary.
            self._install_snapshot_db(snap)
            return file_index, file_term
        if file_index > db_index:
            # Interrupted local snapshot: entries were already applied; only compaction and
            # snapshot metadata remained.
            with self._tx() as conn:
                conn.execute("DELETE FROM log WHERE idx <= ?", (file_index,))
                conn.execute(
                    "UPDATE meta SET value=? WHERE key='snapshot_index'", (str(file_index),)
                )
                conn.execute(
                    "UPDATE meta SET value=? WHERE key='snapshot_term'", (str(file_term),)
                )
            return file_index, file_term
        if file_index != db_index or file_term != db_term:
            # A stale file must never be served to a lagging follower after a newer snapshot
            # was committed. Rebuild at the durable applied boundary.
            self._rebuild_snapshot_at_applied()
            return self.snapshot_index(), self.snapshot_term()
        return db_index, db_term

    # -------------------------------------------------------------------- utility
    def stats(self) -> dict[str, Any]:
        with self._lock:
            log_rows = int(self._conn.execute("SELECT COUNT(*) AS c FROM log").fetchone()["c"])
        size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        wal = self.db_path + "-wal"
        wal_size = os.path.getsize(wal) if os.path.exists(wal) else 0
        return {
            "db_bytes": size,
            "wal_bytes": wal_size,
            "log_rows": log_rows,
            "snapshot_bytes": (
                os.path.getsize(self.snapshot_path)
                if os.path.exists(self.snapshot_path)
                else 0
            ),
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
