# Architecture

## 1. Goals and non-goals

**Goal:** a real, independently running Raft-backed key-value store plus a fault-injection
and observability plane that can kill nodes, partition the network, delay packets and drop
messages, then verify Raft safety properties against logs and state machines collected from
the live nodes.

**Explicit non-goals and simplifications:**

- Fixed cluster membership. There is no online add/remove server or joint consensus.
- No pre-vote. The leader does implement check-quorum and a fresh-quorum (lease-style) read
  check, but not a formally verified clock-based lease.
- No client session deduplication. `SET` and `DELETE` are idempotent state-machine
  operations, so controller retries are safe at the state level.
- Snapshots are single JSON payloads, not chunked or compressed streams.
- No disk corruption, clock skew, CPU/memory pressure or byzantine-node injection.
- The dashboard is unauthenticated and intended for local or trusted-network use.

See [`engineering_decisions.md`](engineering_decisions.md) for the reasoning and rejected
alternatives.

## 2. Runtime topology

```text
                        ┌───────────────────────────────────────────────┐
  React dashboard ──────┤            Cluster Controller (FastAPI)       │
  (React Flow,         │  REST + WebSocket + /metrics + /api/prometheus │
   Recharts)           │  - lifecycle supervisor (spawns processes)     │
                        │  - event store / metrics aggregation           │
                        │  - failure injection control                   │
                        │  - experiment + consistency engine             │
                        └───────┬───────────────┬───────────────┬───────┘
                                │ HTTP (direct, management only) │
                    ┌───────────▼───┐   ┌───────▼───────┐   ┌───▼───────────┐
                    │ node n1       │   │ node n2       │   │ node n3       │
                    │ :7001         │   │ :7002         │   │ :7003         │
                    │ raft.db       │   │ raft.db       │   │ raft.db       │
                    │ snapshot.json │   │ snapshot.json │   │ snapshot.json │
                    └───────▲───────┘   └───────▲───────┘   └───▲───────────┘
                            │                   │               │
                    ┌───────┴───────┐   ┌───────┴───────┐   ┌───┴───────────┐
                    │ proxy :7101   │   │ proxy :7102   │   │ proxy :7103   │
                    │ delay / loss  │   │ delay / loss  │   │ delay / loss  │
                    │ partition     │   │ partition     │   │ partition     │
                    └───────▲───────┘   └───────▲───────┘   └───▲───────────┘
                            └───── all inter-node Raft RPCs ─────┘
```

- Every Raft node is a **separate OS process** with its own port, event loop, SQLite
  database, snapshot file and log file. Nodes never share memory.
- All inter-node RPCs are sent to the **target node's proxy port**. The proxy forwards to
  the target's real port, first applying the link's partition / latency / loss rules. Faults
  therefore affect real bytes on real sockets, not UI state.
- The controller talks to nodes **directly** on their real ports for lifecycle, status,
  log inspection and client operations. Management traffic bypasses the fault layer so an
  operator can still observe an isolated node and prove a partition is real.
- The controller never votes, never appends log entries and never chooses a leader. It only
  discovers the leader by polling `/raft/status` and forwards client requests to it.
- Prometheus scrapes controller state from `/metrics`; a human-facing alias is
  `/api/prometheus`. The frontend uses `/metrics` as its own browser route.

## 3. Component map

| Path | Responsibility |
| --- | --- |
| `backend/config.py` | Settings dataclasses, environment parsing, node/proxy spec construction |
| `backend/main.py` | Controller/API entrypoint |
| `backend/raft/log.py` | In-memory Raft log with snapshot offset, term lookup, truncation |
| `backend/raft/models.py` | LogEntry and typed RPC wire models |
| `backend/raft/persistence.py` | SQLite WAL store: meta, log, KV, apply history, snapshots |
| `backend/raft/state_machine.py` | Deterministic `SET`/`GET`/`DELETE` state machine |
| `backend/raft/rpc.py` | Typed HTTP client for RequestVote / AppendEntries / InstallSnapshot |
| `backend/raft/node.py` | Roles, elections, heartbeats, replication, commit, apply, read-index reads, snapshots |
| `backend/raft/node_app.py` | Per-node FastAPI surface (`/raft/*`, `/kv`, `/health`) |
| `backend/raft/node_main.py` | Node process CLI entrypoint |
| `backend/simulator/network.py` | Mesh proxy with per-link rules and message accounting |
| `backend/simulator/processes.py` | Node process supervisor (spawn / SIGKILL / restart) |
| `backend/simulator/controller.py` | Cluster state, polling, KV gateway, fault controls, metrics, reports |
| `backend/simulator/consistency.py` | Seven live Raft safety checks |
| `backend/simulator/experiments.py` | Six-scenario runner, assertions, benchmark suite |
| `backend/api/app.py` | Public REST/WS API, Prometheus endpoint |
| `backend/api/schemas.py` | Pydantic request validation |
| `backend/monitoring/metrics.py` | Prometheus gauges derived from live controller state |
| `frontend/` | Seven-page observability dashboard |
| `scripts/benchmark.py` | CLI client for experiments and benchmarks |
| `scripts/smoke_test.py` | Commit/read/convergence smoke test |
| `docker/` | Backend/frontend images and nginx reverse proxy |
| `prometheus/` | Scrape configuration |

## 4. Node lifecycle

The controller's `ClusterSupervisor` spawns each node as:

```text
python -m backend.raft.node_main \
  --id n1 --port 7001 --peers '{"n2":"http://127.0.0.1:7102",...}' \
  --data-dir data/node-n1 --host 127.0.0.1 --seed 1
```

The peers map contains **proxy** URLs, so all Raft RPCs traverse the mesh. Each process:

1. opens its SQLite database and reconciles snapshot metadata;
2. loads term/vote, log, KV and applied index;
3. starts a FastAPI/uvicorn server;
4. starts the ticker and apply loop;
5. begins as follower and waits for an election or leader heartbeat.

The supervisor waits for `/health` with `running: true`. Crash injection sends `SIGKILL` to
the process group; restart constructs a new process with the same data directory. The
supervisor tracks PID, crash count, restart count, exit code and log tail.

## 5. Data flow

### Client write (success path)

```text
POST /api/kv {op:set,key,value}
  └─ controller discovers leader from cached /raft/status
      └─ POST leader /kv  (direct, no proxy)
          ├─ leader appends entry to local log, fsyncs (SQLite WAL, synchronous=FULL)
          ├─ per-peer replication loops send AppendEntries through target proxies
          ├─ follower(s) fsync entry, reply match_index
          ├─ leader computes majority matchIndex, applies current-term restriction,
          │  advances commitIndex, applies to KV state machine in one transaction
          └─ responds {committed:true,index,term,leader,result} only after apply
      └─ controller records latency + broadcasts event
```

A write is **never** acknowledged before the leader has applied it. If no majority is
reachable after retries, the request fails with `503`/`504`. The controller does not cache
writes or synthesize success.

### Reads

Reads are forwarded to the current leader. The leader forces a new heartbeat round,
counts acknowledgments only for requests sent after the read began, waits for its applied
index to reach the commit index observed at confirmation, then reads materialized KV state.
The response is tagged `read_mode: "read-index"`. This avoids reusing a pre-read lease that
a competing election may already have invalidated. An isolated minority leader fails
check-quorum and steps down; a leader that cannot complete the fresh round returns `503`
rather than reading stale state.

### Status and events

- Controller polls each node every 350 ms: `/raft/status`, `/raft/events?after=N`.
- Proxy emits `network.*` events (outcome, delay, drop reason) directly into the controller
  event bus; repeated heartbeats are sampled to avoid flooding.
- Controller aggregates a state snapshot and pushes it over `WS /ws/cluster` every 500 ms;
  events are pushed immediately.
- The frontend keeps its own recent-event buffer but renders cluster state exclusively from
  the backend snapshot.

## 6. Persistence model

One SQLite database per node (`data/node-<id>/raft.db`, WAL, `synchronous=FULL`):

| Table | Contents |
| --- | --- |
| `meta` | `current_term`, `voted_for`, `snapshot_index/term`, `applied_index/term`, `node_id` |
| `log` | `idx`, `term`, `kind` (`command`/`noop`), `command_json` |
| `kv` | Materialized key, value, last applied index |
| `apply_history` | Bounded audit trail of `(idx, term, command_hash)` for checker evidence |

Term/vote changes and log appends are committed before the node responds to the RPC that
required them, satisfying Raft's persistence rules. Applying an entry, updating
`applied_index`/`applied_term`, mutating KV, and appending apply history happen in a single
transaction.

`commit_index` is volatile and is recovered as `applied_index` on restart. Commit knowledge
after the applied point is re-learned from the leader. This is safe and ensures no entry is
acknowledged without being applied.

A snapshot is written atomically (`tmp` + fsync + rename + directory fsync) to
`snapshot.json`; log entries at or below the snapshot index are deleted in a transaction
after the file is durable. Startup reconciliation heals a crash between the file rename and
database compaction.

## 7. Raft protocol implementation

- Roles: follower, candidate, leader; randomized election timeout (0.8–1.5 s default),
  200 ms heartbeat, 1.5 s RPC timeout.
- RequestVote: one vote per term, log up-to-date test, term/vote persisted before grant.
- AppendEntries: prev-log consistency check, conflict-index backtracking, conflict
  truncation, commit propagation, heartbeat on empty payload, election-timer reset on valid
  leader RPCs.
- Leader commitment advances only to `N` when a majority has `matchIndex >= N` **and**
  `log[N].term == currentTerm`.
- A no-op entry is appended on election so entries from previous terms can be committed.
- `nextIndex`/`matchIndex` per peer, per-peer single-flight replication loops, immediate
  replication on new entries, heartbeat otherwise.
- Check-quorum: a leader with no majority of fresh acknowledgments for longer than the
  election timeout maximum steps down.
- Reads: leader-only, fresh-majority check within `max(election_timeout_min / 2, 0.1)` s,
  then applied state.
- Snapshots: threshold 128 applied entries (configurable); InstallSnapshot restores a
  lagging follower; installs replace KV, history, log and metadata.
- Membership is fixed at process start. There is no pre-vote or client dedup.

The exact wire format, persistence ordering and failure behaviour are in
[`raft_protocol.md`](raft_protocol.md).

## 8. Consistency checker

While an experiment runs, the checker polls status, log and debug state from every reachable
node and evaluates seven properties:

1. **Election safety** — at most one leader per observed term.
2. **Leader append-only** — per `(leader, term)`, the log prefix never shrinks or changes.
3. **Log matching** — equal `(index, term)` implies identical commands and matching prefixes.
4. **Leader completeness** — entries observed committed appear in later leaders, unless
   covered by a snapshot or committed in a term newer than the leader's term.
5. **State machine safety** — equal applied indexes map to identical command hashes.
6. **Commit safety** — commit indexes are monotonic while uptime increases; applied never
   exceeds commit; commit never exceeds the last log (outside the snapshot base).
7. **Client-ack safety** — a successful client index must be applied on the leader that
   acknowledged it.

Checks record `checked`, `skipped` and `violations`. A check passes only when it executed at
least one comparison and observed no violations. Skips are explicit; unreachable nodes do
not silently produce a pass. Results and sampled evidence are stored in the report.

Sampling is runtime evidence, not a formal proof. Deterministic unit tests and destructive
integration tests cover the protocol rules that the checker observes.

## 9. Public API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/health` | Controller liveness |
| GET | `/api/cluster` | Snapshot: status, health, leader, term, nodes, simulation |
| POST | `/api/cluster/start` / `stop` | Lifecycle |
| GET | `/api/cluster/nodes`, `/api/nodes` | Node list |
| GET | `/api/nodes/{id}` | One node's live status |
| GET | `/api/nodes/{id}/logs` | Node log tail |
| POST | `/api/nodes/{id}/crash` / `restart` | Process fault |
| POST/GET/DELETE | `/api/kv[/{key}]` | Committed KV operations |
| GET | `/api/log` | Leader log with commit markers |
| POST | `/api/simulation/partition` | Set arbitrary partition groups |
| POST | `/api/simulation/heal` | Clear partitions |
| POST | `/api/simulation/latency` | Per-link or global delay |
| POST | `/api/simulation/packet-loss` | Per-link or global loss |
| POST | `/api/simulation/random-failures` | Scheduled seeded crashes |
| POST | `/api/simulation/stop` | Stop random experiment, restart victims |
| POST | `/api/simulation/reset` | Clear all faults and restart victims |
| POST | `/api/simulation/leader-failure` | Crash the current leader |
| GET | `/api/simulation` | Active fault state |
| GET | `/api/metrics` | Rolling metric history |
| GET | `/api/events` | Filtered event timeline |
| GET | `/api/experiments`, `/api/experiments/{id}` | Saved reports |
| POST | `/api/experiments/run` | Run one scenario |
| POST | `/api/benchmarks/run` | Run all six scenarios |
| WS | `/ws/cluster` | Live snapshots + events |
| GET | `/metrics`, `/api/prometheus` | Prometheus exposition |

Input validation lives in `backend/api/schemas.py`: key length, operation pattern, latency
range, loss range, partition coverage, random-failure ranges and experiment duration/rate.

## 10. Dashboard

Seven routes share a WebSocket-backed cluster context:

| Route | Page |
| --- | --- |
| `/` | Overview: health, leader, quorum, term, throughput, latency, recent events |
| `/cluster` | React Flow topology, process cards, log/commit/applied state, RPC edges |
| `/simulator` | Crash/restart, leader failure, partitions, latency, loss, random failures |
| `/kv` | List/read/write/delete committed keys and inspect leader log |
| `/timeline` | Severity/source/node filters, live event stream, expandable payloads |
| `/metrics` | Throughput/latency/availability charts, node counters, benchmark runner |
| `/reports` | Experiment list/detail, assertions, safety checks, event timeline |

Destructive actions use a confirmation dialog. The frontend never infers consensus state:
leader, terms, logs and commit indexes come from backend snapshots. React Flow nodes require
explicit dimensions in this setup; fixed `width`/`height` are set so nodes and edges render.

## 11. Fault injection

| Fault | Mechanism |
| --- | --- |
| Node crash | `SIGKILL` on the node process group via supervisor; port becomes unreachable |
| Restart | Spawn a new process with the same data dir; Raft state reloaded from SQLite |
| Partition | Proxy drops requests whose `(source, target)` pair crosses partition groups |
| Latency | Proxy sleeps before forwarding (one-way delay per request) |
| Packet loss | Proxy drops a configurable fraction using a seeded RNG |
| Random failures | Controller loop selects live nodes, SIGKILLs them, schedules restarts |

See [`failure_scenarios.md`](failure_scenarios.md) for commands, expectations and caveats.

## 12. Health and split-brain detection

The cluster snapshot reports:

- `healthy` — all nodes online, a leader exists, no same-term split brain;
- `degraded` — quorum and a leader exist, but at least one node is unavailable;
- `critical` — below quorum, no leader, or more than one leader observed in the highest term;
- `stopped` — controller cluster is not running.

`split_brain` is a UI/observation flag based on leaders in the same highest term. A correct
Raft cluster should never produce it; if it does, the consistency checker and reports show
the evidence rather than hiding it.

## 13. Deployment

### Local

`make run` starts the controller on `:8000`; it spawns nodes `:7001–7003` and proxies
`:7101–7103`. `make dev-frontend` starts Vite on `:5173` with `/api` and `/ws` proxied to
`:8000`. Data goes to `./data`.

### Docker Compose

`docker-compose.yml` runs:

- `controller` — built from `docker/Dockerfile.backend`, supervises three node processes,
  data volume `raft-data`, API on `:8000`, node/proxy diagnostics bound to loopback;
- `dashboard` — multi-stage Node build served by nginx on `:8080`, proxies `/api` and `/ws`
  same-origin to the controller and serves the SPA shell for deep links;
- `prometheus` — scrapes `controller:8000/metrics`, UI on `:9090`.

The Prometheus config bind mount uses `:ro,Z` because the image runs unprivileged and Fedora
uses SELinux labels. The controller container is a deliberate compromise: process-level
crash injection needs a supervisor with access to child processes. Nodes are still separate
processes with separate ports and storage.

### Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATA_DIR` | `data` | Node dirs, events, reports |
| `CLUSTER_SIZE` | `3` | Odd size >= 3 |
| `NODE_BASE_PORT` | `7001` | First node HTTP port |
| `PROXY_BASE_PORT` | `7101` | First proxy port |
| `AUTO_START` | `true` | Start cluster on controller startup |
| `ELECTION_TIMEOUT_MIN` / `MAX` | `0.8` / `1.5` | Seconds |
| `HEARTBEAT_INTERVAL` | `0.2` | Seconds |
| `RPC_TIMEOUT` | `1.5` | Seconds |
| `SNAPSHOT_THRESHOLD` | `128` | Applied entries between snapshots |
| `CLIENT_TIMEOUT` | `6.0` | Leader commit timeout |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8000` | Controller bind |

## 14. Key risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Raft safety bugs hidden by happy-path tests | Property checker + destructive integration tests (leader kill, partition, snapshot catch-up, checker during failover) |
| Proxy changes latency/failure semantics | Proxy measures and reports every forwarded/dropped message; latency is applied once per request; directed rules are explicit |
| Python event-loop blocking during SQLite fsync | Small writes; `synchronous=FULL` measured; acceptable at demo throughput, documented as a limitation |
| Port collisions in tests | Ephemeral port allocator per fixture, temporary data dirs |
| Frontend showing stale/fake state | UI renders only WS/REST snapshots; no client-side Raft simulation |
| Election churn under heavy latency | Configurable timeouts; documented as expected when delay approaches the election timeout |
| Controller failure orphans nodes | Normal shutdown stops all children; `SIGKILL` of the controller is documented as requiring manual cleanup |
| Weak local security | No auth, loopback node/proxy bindings; dashboard intended for trusted networks |
