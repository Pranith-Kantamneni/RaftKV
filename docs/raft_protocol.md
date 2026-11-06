# Raft Protocol

This document describes the protocol as implemented in `backend/raft/`. It is intentionally
specific about persistence order, commit rules and edge cases; it is not a generic Raft
tutorial.

## 1. State

Each node process has:

| State | Representation | Persisted |
| --- | --- | --- |
| `current_term` | integer | SQLite `meta`, fsynced before RPC replies |
| `voted_for` | node id or none | SQLite `meta`, fsynced before vote replies |
| `role` | follower, candidate, leader | volatile |
| `commit_index` | integer | volatile; recovered as applied index |
| `applied_index` | last committed entry applied to KV | SQLite `meta` in the same transaction as KV |
| `leader_id` | current known leader | volatile |
| log | `(index, term, kind, command)` | SQLite `log`, one row per entry, fsynced before AppendEntries reply |
| state machine | KV map + applied history | SQLite `kv` + `apply_history` |
| snapshot base | `snapshot_index`, `snapshot_term` | SQLite `meta` + `snapshot.json` |

Roles are follower, candidate and leader. A node starts as follower on every process
start. Leadership is never restored from disk.

All node logic runs on one asyncio event loop. SQLite calls are synchronous; `Storage` uses a
reentrant lock and `isolation_level=None` with explicit `BEGIN IMMEDIATE` transactions. The
database uses WAL and `PRAGMA synchronous=FULL`.

## 2. Timing parameters

| Parameter | Default | Notes |
| --- | ---: | --- |
| Election timeout | 0.8–1.5 s | Independent uniform value per election |
| Heartbeat interval | 0.2 s | Replication loop wake-up when idle |
| RPC timeout | 1.5 s | HTTP request timeout |
| Max entries per AppendEntries | 64 | Bounded batch |
| Snapshot threshold | 128 applied entries | Compaction trigger |
| Client commit timeout | 6.0 s | `POST /kv` wait |

The ticker wakes every 20 ms. A leader checks quorum freshness on each tick; followers and
candidates start an election when the deadline passes and no election is already in flight.

## 3. Wire protocol

Inter-node Raft is HTTP/JSON. Every node listens on its direct port. Peers are configured
with **proxy** URLs, and every request includes `X-Raft-From: <node-id>` so the proxy can
apply directed partition, latency and loss rules.

### RequestVote

```json
POST /raft/request-vote
{
  "term": 7,
  "candidate_id": "n2",
  "last_log_index": 42,
  "last_log_term": 6
}
```

```json
{ "term": 7, "vote_granted": true, "voter_id": "n3" }
```

### AppendEntries

```json
POST /raft/append-entries
{
  "term": 7,
  "leader_id": "n2",
  "prev_log_index": 41,
  "prev_log_term": 6,
  "entries": [
    {"index": 42, "term": 7, "kind": "command",
     "command": {"op": "set", "key": "k", "value": "v"}}
  ],
  "leader_commit": 41
}
```

```json
{
  "term": 7, "success": true, "match_index": 42,
  "last_index": 42, "conflict_index": 0, "responder_id": "n3"
}
```

An empty `entries` array is a heartbeat. `kind` is `command` or `noop`.

### InstallSnapshot

```json
POST /raft/install-snapshot
{
  "term": 7,
  "leader_id": "n2",
  "last_included_index": 128,
  "last_included_term": 5,
  "data": {
    "last_included_index": 128,
    "last_included_term": 5,
    "kv": {"a": "1"},
    "history": [{"idx": 128, "term": 5, "command_hash": "..."}]
  }
}
```

The payload is a single JSON snapshot. There is no chunking.

## 4. Election

1. A follower/candidate whose randomized deadline expires becomes candidate, increments
   `current_term`, votes for itself, and persists `(term, vote)` in one transaction.
2. It sends RequestVote to every peer with its last log term and index.
3. A voter handles the request:
   - Lower term: reject with its current term.
   - Higher term: step down, clear vote and persist the new term before evaluating.
   - Up-to-date test: `(candidate_last_term, candidate_last_index) >= (local_last_term,
     local_last_index)`.
   - Vote once per term, or repeat a grant to the same candidate.
   - Persist `(term, voted_for)` before returning the response. Granting a vote also resets
     the election timer.
4. The candidate counts votes including its own. With a majority in the same term and still
   candidate, it becomes leader.
5. Any higher term observed in any RPC or RPC response causes an immediate step-down and
   term persistence.
6. A failed election resets the randomized deadline and emits `election_lost`.

On becoming leader the node:

- initializes `next_index[peer] = last_log_index + 1` and `match_index[peer] = 0`;
- starts one replication loop per peer for that term;
- appends a no-op entry in the new term. The no-op is essential: it lets the leader commit
  entries inherited from earlier terms under the current-term restriction.

## 5. Log replication

For each peer, the leader sends AppendEntries with:

- `prev_log_index = next_index[peer] - 1`, `prev_log_term = log.term_at(prev)`;
- up to 64 entries beginning at `next_index[peer]`;
- its current `commit_index`.

The replication loop is single-flight per peer. New entries set an event that wakes all
loops immediately; otherwise a loop wakes every heartbeat interval.

Follower processing:

1. Reject a lower-term request without changing state.
2. A request with a higher term, or from a leader while this node is candidate/leader,
   causes a step-down to follower with that leader. The term is persisted when it increases.
3. Reset the election deadline only for a valid leader RPC. This prevents election churn
   while heartbeats arrive.
4. If `prev_log_index > last_log_index`, reject with `conflict_index = last_log_index + 1`.
5. If `prev_log_index < snapshot_index`, reply success and report the snapshot index as
   `match_index`.
6. If the local term at `prev_log_index` does not match, reject with a conflict hint. The
   implementation walks backward over the conflicting term to skip that whole term, bounded
   by the snapshot base.
7. For each incoming entry, skip it if already covered by the snapshot. If an entry at the
   same index and term already exists with the same command, skip it. If it differs, emit a
   critical `log_divergence_detected`, truncate from that index, and append the leader's
   version. If an existing entry has a different term, truncate from that index first.
8. Append and fsync each entry before returning success.
9. If `leader_commit > commit_index`, set `commit_index` to
   `min(leader_commit, last_log_index)` and signal the apply loop.

The leader handles a rejection by moving `next_index` down to
`min(previous_next - 1, conflict_hint)`, bounded by the snapshot base, then retries. A success
sets `match_index = max(old_match, response.match_index)` and
`next_index = match_index + 1`.

## 6. Commit rule

The leader computes the candidate commit index by sorting its own `last_log_index` and all
peer `match_index` values and taking the element at position `cluster_size // 2` (the
majority position). It advances `commit_index` only if:

```text
majority_index > commit_index
AND log[majority_index].term == current_term
```

The second condition is Raft's current-term restriction. Entries from previous terms are
committed indirectly when a current-term entry at or after them reaches a majority.

Followers do not independently compute commitment; they take the leader's
`leader_commit`, capped at their own last log index.

A client write future is completed only when the apply loop has applied that exact entry.
The HTTP success response therefore means: durable on a majority, committed, applied
locally, and materialized in the KV state machine.

## 7. Apply path

The apply loop is separate from RPC handling:

- It applies entries in index order from `applied_index + 1` through `commit_index`.
- Applying an entry and advancing `applied_index`/`applied_term` and recording the
  `apply_history` hash happen in one SQLite transaction.
- A pending client future for that index is completed with the committed index, term,
  result and leader id.
- Snapshot creation is considered after each applied entry. If
  `applied_index - snapshot_index >= snapshot_threshold`, a snapshot is created.
- A node that restarts sets `commit_index = applied_index`. Commit knowledge after the
  applied point is volatile and is re-learned from the leader. This is safe and avoids
  acknowledging an entry that was not applied.

## 8. Persistence and restart

Startup order:

1. Open SQLite and create/verify the schema.
2. Reconcile `snapshot.json` against database metadata. If the file is ahead, remove log
   rows at or below the snapshot index and advance snapshot metadata. This heals a crash
   between writing the snapshot file and compacting the log.
3. Load the remaining log, state machine and term/vote.
4. Start as follower. `commit_index` starts at `applied_index`.

Term/vote and log changes are durable before the corresponding RPC reply:

- RequestVote grant: persist vote, then respond.
- AppendEntries append: persist entries, then respond success.
- Higher term observed: persist new term (and cleared vote), then continue.
- Client RPC never returns before local apply.

The SQLite state itself is not replicated; it is reconstructed by the Raft log and
snapshots.

## 9. Snapshots

Snapshots contain the entire materialized KV map and up to 2,000 recent applied-history
records:

```json
{
  "last_included_index": 128,
  "last_included_term": 5,
  "kv": {"key": "value"},
  "history": [{"idx": 128, "term": 5, "command_hash": "abc123"}]
}
```

Creation order:

1. Write `snapshot.json.tmp`, flush, `fsync`.
2. `os.replace` to `snapshot.json`.
3. `fsync` the directory.
4. In one transaction, update `snapshot_index`/`snapshot_term` and delete log rows through
   the snapshot index.

If the process dies between steps 3 and 4, startup reconciliation completes the compaction.

A leader whose `next_index[peer] <= snapshot_index` sends InstallSnapshot. The follower:

1. rejects a stale term;
2. installs the snapshot file atomically;
3. replaces the KV table, apply history, and entire log; updates snapshot/applied metadata;
4. resets its in-memory log with the snapshot base;
5. reports the snapshot index as `match_index`.

Discarding the follower's entire log is conservative; the leader resends any entries after
the snapshot boundary.

## 10. Client operations

### Writes

`POST /kv` on a leader appends the command, waits for commit and local apply, then returns:

```json
{
  "committed": true,
  "index": 17,
  "term": 4,
  "leader": "n1",
  "result": {"op": "set", "key": "k", "value": "v"}
}
```

Errors:

| Condition | Status |
| --- | --- |
| Not leader | `409` with `leader` hint |
| No majority / apply timeout | `504` |
| Invalid request | `422` |

The public controller maps unavailable quorum/leader conditions to `503` for clients.

### Reads

Reads are leader-only and use a ReadIndex-style fresh-heartbeat round:

1. Reject if not leader (`409`).
2. Record the read start time and trigger a new heartbeat/replication round. Peer
   acknowledgments obtained before the read started do **not** count.
3. Count acknowledgments for requests sent after the read began within
   `max(election_timeout_min / 2, 0.1)` seconds. The local node counts as one. If fewer than
   a majority acknowledged, return `503` without reading.
4. Re-check role and term after waiting for the round.
5. Wait up to 1 s for `applied_index` to reach the `commit_index` observed at confirmation.
6. Return the materialized value and `read_mode: "read-index"`.

The fresh round gives the read a linearization point after its invocation: a competing
leader elected later can only commit later writes, which the read may be ordered before. A
leader already deposed before the read cannot obtain majority acknowledgments in its old
term because the new leader's voters have a higher term. This is paired with check-quorum:

- A leader tracks `last_quorum_time` using heartbeat send times.
- If no majority of fresh acknowledgments exists for longer than the election timeout
  maximum, it steps down and stops claiming leadership.
- As a result, an isolated minority leader cannot keep serving reads after the freshness
  window, and a read never relies on a lease window alone.

## 11. Failure behaviour

### Leader crash

A majority elects a new leader in a higher term. The new leader's log contains every
committed entry (Leader Completeness), and its no-op entry permits commitment of inherited
entries. The old leader, when restarted, becomes a follower with a stale term and receives
AppendEntries/InstallSnapshot until it converges.

### Minority partition

The isolated leader cannot reach a majority. Its writes time out; its lease-style reads fail
the quorum check; check-quorum makes it step down. The majority elects a new leader. On heal,
the old leader truncates any uncommitted suffix and adopts the majority history.

### Follower crash

A majority still commits. When the follower restarts, it reloads its durable log and is
caught up by AppendEntries. If it is far behind and its next entry is below the leader's
snapshot base, InstallSnapshot replaces its state.

### Message loss and latency

A dropped RPC is a network error or a simulated `503`; the protocol retries. Elections can
time out and retry in a higher term. Safety does not depend on message delivery, only on
majority intersection and durable logs.

## 12. Why the safety properties hold

- **Election Safety:** a candidate needs a majority of votes in one term; each voter grants
  at most one vote per term and persists it.
- **Leader Append-Only:** leaders only append to their own logs; they truncate followers,
  never their own committed entries.
- **Log Matching:** AppendEntries checks `(prev_log_index, prev_log_term)` and repairs
  conflicts before appending; equal index/term implies the same command in this
  implementation, and divergent equal-index/term data is treated as a critical bug.
- **Leader Completeness:** voter up-to-date checks ensure a leader has all committed entries;
  the current-term commit rule prevents commitment by counting stale-term entries alone.
- **State Machine Safety:** only committed entries are applied, in index order, and apply is
  atomic with the applied index.
- **Commit Safety:** commit only advances on a majority match, never beyond the last log
  index, and client success is emitted only after local apply.

The runtime checker samples these properties against live nodes. Sampling is strong
evidence, not a formal proof; the deterministic unit and destructive integration tests cover
the protocol rules that make the properties hold.

## 13. Deliberate protocol non-goals

- No pre-vote. A partitioned node may increment terms until it learns otherwise.
- No dynamic membership or joint consensus.
- No client request deduplication/session tracking.
- No chunked snapshot transfer, compression or streaming.
- No disk-fault or clock-skew model.
- The fresh-quorum read path is not a fully formalized lease with clock assumptions.
