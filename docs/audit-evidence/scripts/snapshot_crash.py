import json, os, tempfile
from backend.raft.models import LogEntry
from backend.raft.persistence import Storage
from backend.raft.state_machine import KVStateMachine

tmp = tempfile.mkdtemp(prefix="raftkv-snap-")
s = Storage(tmp, "n1")
# local applied state: k=old through index 2
s.apply_entry(LogEntry(1, 1, "noop"))
s.apply_entry(LogEntry(2, 1, "command", {"op": "set", "key": "k", "value": "old"}))
s.create_snapshot(2, 1, {"k": "old"}, [
    {"idx": 1, "term": 1, "command_hash": "a"},
    {"idx": 2, "term": 1, "command_hash": "b"},
])
print("before crash", s.applied_index(), s.applied_term(), s.snapshot_index(), s.snapshot_term(), s.list_kv(), s.log_count())
# Simulate install_snapshot from a leader at index 6, crash after durable file write
new_payload = {
    "last_included_index": 6,
    "last_included_term": 3,
    "kv": {"k": "new", "only-in-snapshot": "yes"},
    "history": [{"idx": 6, "term": 3, "command_hash": "c"}],
}
s._write_snapshot_file(new_payload)
s.close()

# Restart recovery
s2 = Storage(tmp, "n1")
idx, term = s2.reconcile_snapshot_on_startup()
sm = KVStateMachine(s2)
print("after restart", "reconcile", (idx, term), "applied", (sm.applied_index, sm.applied_term), "snapshot", (s2.snapshot_index(), s2.snapshot_term()), "kv", s2.list_kv(), "log_rows", s2.log_count(), "history", s2.applied_history())
print("EXPECTED", {"k": "new", "only-in-snapshot": "yes"}, "at applied 6 term 3")
print("BUG" if s2.list_kv() != {"k":"new", "only-in-snapshot":"yes"} or sm.applied_index != 6 or sm.applied_term != 3 else "OK")
s2.close()
