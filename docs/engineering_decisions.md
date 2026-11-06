# Engineering Decisions

This is a decision record for the choices that shape correctness, operability and the
honesty of the reported results. Each entry states the decision, why, and what was accepted
or rejected.

## ADR-1: Implement Raft from scratch instead of wrapping a library

**Decision:** implement log, elections, replication, commit, apply, snapshots and HTTP RPCs
in this repository.

**Why:** the goal is a failure lab that demonstrates the protocol, not a wrapper around an
opaque consensus library. Every event, log entry and safety check is inspectable.

**Trade-off:** more code and more opportunities for subtle bugs. Mitigation is deterministic
unit tests, destructive live-process tests, and a runtime checker. We do not claim formal
verification.

**Rejected:** etcd/Consul embedded mode. It would hide the protocol and make the simulator a
UI over someone else's state.

## ADR-2: One OS process per node

**Decision:** start each node as a separate Python process with its own HTTP port, proxy
port, data directory, SQLite database, snapshot file and log file. Integration tests do the
same.

**Why:** real crashes, restarts, port loss and process isolation are central requirements.
An in-process object graph cannot prove those behaviours.

**Trade-off:** process startup and port management are more complex, and tests are slower
(~20 s for the suite). The controller owns a supervisor rather than a list of objects.

**Rejected:** threads/async tasks in one process as the primary deployment. They may be
useful for unit simulation but were not used for integration tests.

## ADR-3: SQLite per node with WAL and `synchronous=FULL`

**Decision:** persist term/vote, log, KV and apply history in SQLite per node. Use WAL,
`synchronous=FULL`, explicit `BEGIN IMMEDIATE` transactions, and a lock around the
connection.

**Why:** SQLite gives atomic multi-row transactions and real fsync without adding a database
service. `synchronous=FULL` means a successful commit is durable rather than merely written
to the OS page cache. The applied index, KV mutation and apply-history row are committed
together, which makes restart consistent.

**Trade-off:** fsync on every append and apply limits throughput. This is visible in the
benchmark and accepted for a correctness-focused lab. `Storage` calls are synchronous and
block the event loop briefly.

**Rejected:** an in-memory dict plus an append-only JSON file. It lacks transactional
atomicity and makes recovery bugs likely. Rejected also: a shared database, because nodes
must fail independently.

## ADR-4: HTTP/JSON inter-node RPC

**Decision:** use FastAPI/httpx with JSON payloads for RequestVote, AppendEntries and
InstallSnapshot.

**Why:** simple to inspect, test and proxy. The mesh proxy can read `X-Raft-From`, classify
the RPC from the path/body, and apply directed faults without implementing a binary
protocol.

**Trade-off:** HTTP and JSON overhead lowers throughput and increases latency relative to a
binary protocol. It also makes it easy to accidentally expose endpoints without auth.

**Rejected:** raw sockets or gRPC. They would improve performance but complicate the proxy
and debugging. Performance is not the primary goal.

## ADR-5: The controller is not a Raft participant

**Decision:** the controller spawns processes, polls status, forwards clients, injects
faults and runs checks. It never votes, appends entries, stores the authoritative KV state,
or chooses a leader.

**Why:** the dashboard must observe consensus rather than become part of it. If the
controller dies, the Raft cluster can continue. Source of truth remains node logs and state
machines.

**Trade-off:** client operations require an extra hop through the controller, and leader
discovery is eventually consistent (polling). A stale snapshot can produce a `409`, which
the controller retries on another attempt.

**Rejected:** terminating client requests at a leader chosen by the controller. That would
create a second source of truth.

## ADR-6: SIGKILL and a supervisor, not a graceful "simulate crash" flag

**Decision:** crash injection sends `SIGKILL` to the node process group. Restart launches a
new process from the same data directory.

**Why:** a graceful shutdown could flush state and conceal recovery bugs. SIGKILL leaves
whatever SQLite already fsynced, exactly what the persistence rules must handle.

**Trade-off:** SQLite WAL files and log files may need reconciliation on startup. A killed
controller can orphan node processes; the supervisor uses process groups so normal shutdown
can clean up. This is documented as an operational caveat.

**Rejected:** a `crash()` method that closes storage cleanly. It would not be a real crash.

## ADR-7: Fault injection at per-target mesh proxies

**Decision:** each node has a proxy; all inter-node RPCs target the destination proxy. The
proxy enforces partitions, latency and loss over real HTTP sockets. Controller management
traffic bypasses proxies and goes directly to node ports.

**Why:** this makes faults affect the protocol, not just the UI. A partition really prevents
Raft messages. Management bypass keeps an isolated node inspectable and lets the checker
prove what it knows.

**Trade-off:** an extra HTTP hop for every Raft RPC; proxy behavior becomes part of the
performance measurement. The proxy is deliberately simple and single-process.

**Rejected:** a network namespace or tc rules. Those are closer to real networking but much
harder to make deterministic, seeded and inspectable in this environment.

## ADR-8: No-op entry on election

**Decision:** every newly elected leader appends a no-op entry in its term before/alongside
serving writes.

**Why:** Raft cannot commit entries from earlier terms based only on their replication; a
current-term entry is required. The no-op creates that entry immediately, allowing inherited
entries to commit even when no client write arrives.

**Trade-off:** one extra log entry per election. It is visible in the UI and reports.

**Rejected:** relying on the first client write. A cluster with no traffic could leave
inherited entries uncommitted indefinitely, confusing recovery analysis.

## ADR-9: Check-quorum plus fresh-heartbeat ReadIndex reads

**Decision:** a leader tracks peer acknowledgments by the time the heartbeat was sent. If no
majority of fresh acknowledgments exists for longer than the election timeout maximum, it
steps down. A read starts a new heartbeat round, counts only acknowledgments for requests
sent after the read began, then waits for applied state to reach the commit index observed
at confirmation. The response reports `read_mode: "read-index"`.

**Why:** without check-quorum, a leader isolated in a minority can keep answering reads from
stale state for an unbounded period. Reusing pre-read acknowledgments is also unsafe: a
follower may vote for a higher-term candidate before its own election timer expires. A new
heartbeat round after the read invocation provides a defensible linearization point before
any later leader can commit a write the old leader has not seen.

**Trade-off:** every read costs one heartbeat round-trip and can return `503` under very high
injected latency. That is a correctness-preserving failure, not a stale success. The
implementation does not claim a clock-based lease or a machine-checked proof.

**Rejected:** serving reads from any leader without quorum confirmation. Also rejected:
implementing a full clock-based lease; it would add assumptions not needed for the lab.

## ADR-10: Conflict index backtracking

**Decision:** AppendEntries rejection includes a `conflict_index` hint. The follower walks
back over the conflicting term and reports a lower retry point. The leader clamps to the
snapshot base.

**Why:** naive one-entry-at-a-time backtracking is slow with a long divergent suffix.
Conflict hints reduce retries after rejoin.

**Trade-off:** more follower logic and a wire field not in the minimal Raft paper. Tests
cover conflicting suffix truncation and snapshot boundary behavior.

## ADR-11: Snapshot as one JSON payload with conservative installation

**Decision:** snapshots contain the full KV map and recent applied history, written
atomically to `snapshot.json`; log entries through the snapshot index are deleted.
InstallSnapshot writes the file, then replaces KV, apply history and the entire log.

**Why:** simple, inspectable, sufficient for the data sizes in this project. Atomic
file write plus restart reconciliation avoids a half-snapshot state. Discarding the entire
follower log is conservative and lets the leader resend entries after the snapshot boundary.

**Trade-off:** snapshots are not chunked or compressed; a large state means a large RPC and
a potentially long pause. It is not suitable for production-scale state.

**Rejected:** chunked snapshot streaming and incremental snapshots. They would add
significant protocol surface without changing the safety lessons.

## ADR-12: Runtime consistency checker with explicit skips

**Decision:** implement seven live checks and record `checked`, `skipped` and `violations`
per check. A check passes only when it has at least one comparison and zero violations.
Unreachable nodes cause skips, not automatic success.

**Why:** the user needs evidence during failures, not just happy-path tests. Explicit counts
prevent a report from claiming a check passed when no evidence existed.

**Trade-off:** sampling can miss a transient violation; it is not a formal proof. The checker
also has to handle legitimate partition states (e.g. an old leader missing future committed
entries) with narrow, documented exclusions.

**Rejected:** a single pass/fail bit. It hides whether a check actually ran.

## ADR-13: Real experiments with assertions, no synthetic numbers

**Decision:** scenario reports are generated only from executed runs. Benchmarks call the
same experiment runner. Reports include hardware, method, assertions, safety counts and
events.

**Why:** dashboards that invent throughput or safety are worse than no dashboard. The
published results include failed writes and slow tails rather than smoothing them.

**Trade-off:** benchmark output depends on the host and is noisy. Short single-host runs are
not capacity planning data. Raw reports are retained so readers can inspect them.

**Rejected:** precomputed demo data or a "replay" mode. Both would violate the project's
core requirement.

## ADR-14: React + React Flow + Recharts dashboard

**Decision:** React 18, TypeScript, Vite, Tailwind, React Flow 11 and Recharts 2, with a
WebSocket-backed cluster context and typed REST client.

**Why:** React Flow is a natural fit for topology and live RPC edges; Recharts covers
latency/throughput charts; Tailwind keeps the dark console styling consistent.

**Trade-off:** the production bundle is ~853 kB (238 kB gzip) because chart and flow
libraries are heavy. Node dimensions must be explicit for React Flow visibility; this was a
real bug fixed during validation. Recharts 2 is now an older major, documented as such.

**Rejected:** a custom canvas graph and hand-rolled charts. They would add maintenance cost
without improving the core protocol demonstration.

## ADR-15: One controller container supervising node processes in Docker

**Decision:** Docker Compose runs one controller container that supervises three node child
processes, plus a separate nginx dashboard and Prometheus. Nodes remain separate processes
with separate ports and storage inside the shared container.

**Why:** crash injection requires process control. Running each node in a separate container
would require Docker API access or a privileged sidecar to kill/restart peers. The chosen
layout keeps real `SIGKILL` semantics with a simple deployment.

**Trade-off:** the controller container is a blast radius for all nodes. This is explicitly
a local demo deployment, not a highly available production scheduler. Node and proxy ports
are bound to loopback for diagnostics only.

**Rejected:** Kubernetes/Compose per-node containers with a controller that shells into the
Docker daemon. That adds privileges and complexity beyond the lab's goal.

## ADR-16: Fixed membership, no client dedup, no pre-vote

**Decision:** the cluster size is fixed at startup; there is no dynamic membership, no
client session deduplication, and no pre-vote.

**Why:** these features are large protocol extensions. The core requirements are elections,
replication, persistence, recovery, snapshots and fault observation. `SET`/`DELETE` are
idempotent, so client retries are safe at the state-machine level. Fixed membership keeps
configuration and quorum logic simple.

**Trade-off:** a retried write after a timeout may append a duplicate log entry (same final
state). A partitioned node may start elections and raise terms until it hears from a leader
(no pre-vote). Membership changes require a restart with a new configuration and are not
supported online.

## ADR-17: Two Prometheus paths

**Decision:** keep `/metrics` as the Prometheus scrape endpoint and add `/api/prometheus` as
an alias for humans. The frontend's `/metrics` route remains a dashboard page.

**Why:** Prometheus conventionally scrapes `/metrics`, while React Router needs `/metrics`
for the Metrics page. A dev proxy or nginx route collision made the dashboard page
unreachable. The alias lets the UI link to the text exposition without hijacking the route.

**Trade-off:** one extra API path. Prometheus still scrapes the canonical path.

## ADR-18: Local-first security and test environment

**Decision:** no authentication/authorization, no TLS, permissive CORS, all processes bound
to localhost in the local workflow, and node/proxy ports loopback-only in Compose.

**Why:** this is a local observability and failure lab. Adding auth would complicate the
demo without improving the consensus lesson.

**Trade-off:** do not expose it to an untrusted network. The Docker controller API is bound
to `127.0.0.1`, but the dashboard is on `0.0.0.0:8080` by default for convenience; change
`DASHBOARD_PORT`/bindings before exposing it.

## ADR-19: Publish short, honest benchmarks with raw reports

**Decision:** publish a 10 s, 6 ops/s suite and commit the raw JSON reports, including failed
writes and the latency/loss tails. Clearly state single-host, Python, open-loop limitations.

**Why:** the alternative is a polished but misleading number. The interesting result is
safety and recovery under faults, not peak throughput. Raw reports let a reader verify the
tables.

**Trade-off:** the numbers are modest. They should not be compared directly with production
databases or used for sizing.

**Rejected:** hiding the failed writes, averaging away p99, or running on an idle machine
and presenting it as general capacity.
