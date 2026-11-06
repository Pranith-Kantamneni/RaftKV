# Release audit — Raft KV failure simulator

**Audit date:** 2026-11-01  
**Auditor:** OpenCode (read-only analysis plus isolated test clusters; no live-user traffic)  
**Scope:** Raft safety and liveness, persistence and recovery, API validation, Prometheus
metrics, dashboard behaviour, Docker deployment, dependency security, and performance.  
**Artifact:** Existing Docker volume and `docs/results/` benchmark reports were preserved.
A volume backup was taken before destructive testing.

## Verdict

**Conditional pass for an open-source demo / portfolio release. Not production-ready.**

Two Critical correctness defects were reproduced and fixed: an isolated old leader could
serve stale reads, and an interrupted snapshot install could leave `applied_index` ahead
of the durable KV state. The final read path fails closed, the snapshot path is
transactional and self-healing, and the full test/fault matrix passes.

Production deployment is still blocked by the absence of authentication, root containers,
unbounded event-log files, the lack of a formal linearizability proof or disk/clock fault
model, and the intentionally limited snapshot/client-dedup implementation. Those are
listed under **Residual risks** rather than hidden.

**Suggested badge:** `v1.0.0-demo`, not `production-ready`.

## Method

1. Read the Raft implementation against the election, log-matching, leader-completeness,
   state-machine-safety and current-term commit rules.
2. Built isolated 3-node and 5-node clusters on ephemeral ports. Killed processes with
   `SIGKILL`, isolated proxies, partitioned leaders/minorities, restarted whole clusters,
   and inspected durable state after recovery.
3. Ran a history-based linearizability checker alongside concurrent writers and readers,
   including a run with a leader crash.
4. Reproduced candidate read and snapshot failures before changing code, then reran the
   same reproductions after the fix.
5. Exercised the API, Prometheus endpoint, nginx dashboard, browser routes and major
   interactions. The deployed stack was rebuilt without deleting its named volume.
6. Audited Python and npm dependencies, Docker privileges/bindings, CORS, input limits,
   security headers and destructive endpoints.
7. Measured normal-load performance and read/write saturation. No benchmark values were
   edited or invented.

Environment: Fedora 44, kernel 7.1.3, Python 3.14.6, Node 22.23.2, Docker 29.6.0,
Compose 5.4.0, Intel i5-1240P, 16 logical CPUs, 15 GiB RAM.

## Findings summary

| ID | Severity | Finding | Status |
| --- | --- | --- | --- |
| C-1 | Critical | Isolated old leader served stale reads with HTTP 200 | Fixed and reproduced before/after |
| C-2 | Critical | Interrupted InstallSnapshot left applied index ahead of durable KV state | Fixed and reproduced before/after |
| H-1 | High | Read returned state behind its own confirmed commit index during apply lag | Fixed and reproduced |
| H-2 | High | Read could complete after the node had stepped down | Fixed and reproduced |
| H-3 | High (code review) | AppendEntries gap could be acknowledged past a missing entry | Fixed, regression-tested; no pre-fix runtime artifact retained |
| H-4 | High (hardening) | Invalid command op could become committed and wedge the apply loop | Fixed, regression-tested; not observed in normal workloads |
| H-5 | High (deployment) | Dashboard (and thus the destructive API proxy) bound to all host interfaces | Fixed to loopback defaults |
| M-1 | Medium | Wildcard CORS on destructive API endpoints | Fixed; CORS is opt-in and never wildcard |
| M-2 | Medium | No Raft/API timeout and cluster-size validation | Fixed; invalid settings fail fast |
| M-3 | Medium | Long keys were accepted on read/delete paths; no nginx body limit | Fixed; API and proxy limits added |
| M-4 | Medium | Metrics/UI described commit-index growth as generic throughput/ops | Fixed labels and counter semantics |
| M-5 | Medium | Frontend dependency advisories (2 critical, 1 high, 5 moderate) | Fixed; npm audit 0 |
| M-6 | Medium | Missing browser security headers | Fixed in nginx |
| M-7 | Medium | KV keys/values are exposed through events, `events.jsonl` and URL paths | Accepted/documented; not a secrets store |
| L-1 | Low | Prometheus lifecycle/admin API enabled | Removed |
| L-2 | Low | Controller/dashboard containers run as root | Mitigated with `no-new-privileges`; not fully fixed |
| L-3 | Low | No API authentication | Accepted for loopback-only demo; documented |
| L-4 | Low | Event JSONL grows without rotation | Accepted; documented |
| L-5 | Low | Role metric is not quorum-aware and resets on process restart | Accepted; documented |
| L-6 | Low | Large frontend bundle; Recharts 2 deprecation warning | Accepted; no CVE |
| L-7 | Low | Snapshot uses a single payload, no chunking/compression | Accepted non-goal |
| L-8 | Low | No client deduplication / exactly-once semantics | Accepted non-goal; set/delete are idempotent |
| L-9 | Low | Runtime checker is sampled evidence, not a formal proof | Accepted; stated explicitly |

## Critical findings

### C-1 — Stale reads from an isolated old leader

**Location:** `backend/raft/node.py` read path (`read_key`, `_confirm_quorum`) and the
replication acknowledgement tracking in `_replicate_once`.

**Observed before the fix** (`docs/audit-evidence/stale-read-before.log`):

```text
initial leader n1
baseline committed index=2 term=1 value=old
new leader n3 after 0.681s
new committed index=4 term=2 value=new
old read 0 200 {"key":"lease","value":"old","index":2,"commit_index":2,
                "term":1,"read_mode":"leader-lease"}
STALE_READ_REPRODUCED
```

The old leader had been isolated from the majority, a new leader had committed a newer
value, and the old leader still answered a client read with the old value. This violates
linearizability: the read completed after the newer write was acknowledged.

**Cause:** the read path trusted the leader role and a pre-existing heartbeat/lease
window. It did not require a majority acknowledgement that was sent *after the read
began*. An isolated leader could therefore reuse old acknowledgements.

**Fix:** every read now starts a fresh confirmation round. `last_ack[peer]` records the
send time, not the receive time; `_count_fresh_acks` only counts acknowledgements sent at
or after `read_started` and within the confirmation window. The node rechecks its role and
term after waiting, and waits for the local state machine to reach the confirmed commit
index. If it cannot confirm a majority or apply in time, it returns `503`.

**Verification:** ten consecutive reads against the same isolated old leader all returned
`503` with `leader could not confirm a fresh majority`; no stale value was observed
(`docs/audit-evidence/stale-read-final.log`). The fault suite’s partitioned-leader scenario
also reports `stale_read_observed: false` and `old_leader_read_status: 409`.

**Trade-off:** an isolated or very slow leader now fails reads instead of serving
potentially stale data. This is the intended safety/liveness choice.

### C-2 — Interrupted snapshot install could restore the wrong state

**Location:** `backend/raft/persistence.py` snapshot file/DB handling; the install handler
in `backend/raft/node.py`.

**Observed before the fix** (`docs/audit-evidence/snapshot-crash-before.log`):

```text
before crash: applied=2 term=1 commit=2 snapshot=1 kv={'k':'old'}
after restart: applied=(6,1) snapshot=(6,3) kv={'k':'old'}
expected:     kv={'k':'new','only-in-snapshot':'yes'} at applied 6 term 3
BUG
```

The snapshot file had been written but the process died before the database half of the
install committed. On restart the code trusted the snapshot boundary from the file while
KV state still came from the older database. Applied metadata and data disagreed.

**Cause:** the snapshot file and SQLite state were not installed as one recoverable
operation; startup did not reconcile a file that was newer than the database boundary.

**Fix:**

- Validate snapshot metadata, KV map, monotonic history and term before use.
- Write the snapshot file atomically (`tmp` + `fsync` + `rename`) and only then commit the
  KV/history/log/meta replacement in one SQLite transaction.
- On startup, if the file is newer than `applied_index`, install it; if a valid snapshot
  exists but SQLite state is behind, reconcile to the file; if the file is corrupt or
  stale, rebuild it from the durable applied prefix.
- Serialize snapshot writes and preserve the snapshot base when compacting the log.

**Verification:** the same crash point now recovers `applied=(6,3)`, snapshot `(6,3)`,
the expected KV values, empty log rows after compaction, and the expected history
(`docs/audit-evidence/snapshot-crash-final.log`). The regression suite additionally covers
corrupt-file rebuild and metadata mismatch rejection.

## High findings

### H-1 — Read ahead of applied state

Before the fix, a leader could return a value with `index: 1` while its confirmed
`commit_index` was 5 (`docs/audit-evidence/read-bugs-before.log`). The read was not
linearized at the confirmed index. The final path waits for `applied_index >= target` and
returns `503` if the state machine does not catch up within the deadline
(`READ_WITH_LAGGING_APPLY rejected in 1.002 s`).

### H-2 — Read completing after step-down

Before the fix, a read could finish with a successful old value after the node had already
become a follower and observed a higher term. The current read path rechecks role and term
after quorum confirmation. The reproduction now returns `NotLeaderError`
(`docs/audit-evidence/read-bugs-final.log`).

### H-3 — AppendEntries gap acknowledgement (code-review finding)

The AppendEntries handler now verifies that every supplied entry is exactly
`matched_index + 1`; a gap breaks before persistence and is never acknowledged past the
missing entry. Batches are persisted in one transaction, and duplicates are idempotent.
`test_append_entries_never_acknowledges_a_gap` and
`test_duplicate_append_entries_is_idempotent` pin this down. This was found by inspection;
no pre-fix runtime log was retained, so it is not claimed as a reproduced incident.

### H-4 — Invalid command validation (hardening)

Commands entering the replicated log are now validated for `op`, key length/type and set
value type/length in `_validate_log_command`. An unknown op could previously be committed
and then raise inside the apply loop, stopping state-machine progress. The regression test
`test_append_entries_rejects_unknown_command_op` proves the entry is rejected before it
reaches the log. Normal client traffic already used valid commands; this is defense against
a corrupt peer or direct RPC caller.

### H-5 — Host bindings

The pre-fix dashboard published `0.0.0.0:8080` and Prometheus `0.0.0.0:9090`; because the
dashboard proxies the destructive simulator API, that exposed more than telemetry on the
LAN. Final Compose bindings are loopback-only by default, including node/proxy ports,
controller, dashboard and Prometheus. Final state is in
`docs/audit-evidence/compose-ps-final.txt`; all published edges are `127.0.0.1`.

## Medium findings

### M-1 — Wildcard CORS

Before: `security-before.txt` shows
`access-control-allow-origin: *` on a preflight request. That allowed any web origin to
call the simulator API from a browser. Final code only adds CORS when `CORS_ORIGINS` is
explicitly set, splits/trims the allow-list, sets `allow_credentials=False`, and never
uses `*`; an explicit `*` in `CORS_ORIGINS` raises at startup.
`test_cors_is_opt_in_and_never_wildcard` confirms an allowed origin receives the
configured header while an untrusted origin does not, and
`test_cors_rejects_wildcard_at_startup` pins the fail-fast path. Final unauthenticated
preflight returns `405` without an allow-origin header.

### M-2 — Configuration validation

`RaftSettings.__post_init__` now rejects non-positive election, heartbeat, RPC and client
timeouts, `heartbeat_interval >= election_timeout_min`, negative snapshot thresholds, and
`max_append_entries < 1`. `ClusterSettings.build_nodes` rejects cluster sizes below 3 and
even-sized clusters. Invalid settings fail at startup rather than producing unsafe timers
or indexes.

### M-3 — Key/body limits

Controller/API validation now requires keys of 1–256 characters and values of at most
100,000 characters; the node path enforces the key limit too, and nginx caps request
bodies at 1 MiB. Before, a 300-character key read returned `200`; final returns `422`
(`api-security-metrics-after.log`). Oversized values also return `422`.

### M-4 — Metrics semantics

The commit-rate metric is now
`raft_cluster_committed_entries_per_second`, with help text stating that it is leader
commit-index growth and includes no-op entries. Client writes use cumulative
`raft_client_successful_writes_total` / `raft_client_failed_writes_total` counters. The
dashboard labels are “Commit rate” with unit `entries/s`, and “Successful writes”, rather
than calling commit growth generic throughput or ops. Node RPC/election counters, snapshot
index, network drop/forward counters and availability/health gauges are exported. The
Prometheus target and final label rendering were checked against the deployed build
(`ui-labels-final.txt`).

Verified exposition units and types (`metrics-final.txt`):

| Metric | Type / unit |
| --- | --- |
| `raft_node_up`, `raft_node_is_leader`, `raft_cluster_has_leader` | Gauge, `0`/`1` |
| `raft_node_term`, `raft_node_*_index`, `raft_cluster_*_index` | Gauge, Raft index/term integers |
| `raft_node_role` | Gauge with `node` and `role` labels (one-hot) |
| `raft_node_rpc_failed`, `raft_node_elections_started`, `raft_node_elections_won` | Gauge, cumulative since node start |
| `raft_cluster_committed_entries_per_second` | Gauge, leader commit-index growth per second (includes no-ops) |
| `raft_cluster_write_latency_p95_ms`, `raft_cluster_replication_latency_ms`, `raft_cluster_network_latency_ms`, `raft_cluster_last_election_ms` | Gauge, milliseconds |
| `raft_cluster_availability_ratio` | Gauge, active nodes / total nodes (0–1) |
| `raft_cluster_health` | Gauge, `1` healthy / `0.5` degraded / `0` critical |
| `raft_client_successful_writes_total`, `raft_client_failed_writes_total` | Counter, acknowledged / exhausted client writes |
| `raft_network_forwarded_messages`, `raft_network_dropped_messages` | Gauge, cumulative simulator messages |

A crashed node reported `raft_node_up{node="n2"} 0.0`; a partition exposed failed RPC
counts on the isolated leader; and the client Counter was checked against controller
totals (`api-security-metrics-after.log`). Per-node counters are exported as gauges
because the values are absolute node counters scraped from the controller, not
process-local Prometheus events.

### M-5 — Dependencies

Baseline frontend audit: **8 advisories — 2 critical, 1 high, 5 moderate**. Most were in
development tooling (Vite/Vitest/esbuild); `react-router-dom` also needed a production
upgrade. Upgrades: React Router `7.18.5`, Vite `8.3.5`, Vitest `5.0.5`,
`@vitejs/plugin-react` `6.1.2`. Final `npm audit` reports **0 vulnerabilities**, and
`pip-audit -r requirements.txt` reports **0 known vulnerabilities**. Major upgrades were
validated with typecheck, 16 frontend tests, production build, route smoke tests and
browser interactions. Node 22.12+ is now required (Docker uses Node 22).

### M-6 — Browser security headers

nginx now sets `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy:
no-referrer`, a restrictive CSP, `client_max_body_size 1m`, and disables server tokens.
The deployed response was checked and all headers were present.

### M-7 — KV data is observable by design

The project emits the full command object in `client_write_received` and `entry_applied`
events, broadcasts those events over WebSocket, returns them from `/api/events`, appends
them to `events.jsonl`, and exposes GET/DELETE keys in request paths. There are no
credentials or API tokens in the default stack, but **KV keys and values should be treated
as visible to anyone who can read events or logs**. This is intentional simulator
observability, not a bug in the Raft path; it is a deployment limitation and should be
changed (redact values or gate event access) before using the store for secrets.

## Low findings and accepted risks

- **Prometheus lifecycle API:** `--web.enable-lifecycle` was removed; Prometheus remains
  loopback-bound with `no-new-privileges`.
- **Root containers:** controller and dashboard still run as root (Prometheus runs as
  `nobody`). `no-new-privileges:true` is set for all services. A non-root conversion was
  not forced because the named volume and file ownership predate this audit; this remains
  a production hardening item.
- **No authentication/TLS:** every API and simulator endpoint is unauthenticated. The
  default loopback binding is the only access control. Do not publish these ports, and
  use an authenticated reverse proxy/SSH tunnel if remote access is required.
- **Unbounded event file:** in-memory event deques are bounded (1,000 node events, 5,000
  controller events), but `events.jsonl` is appended and flushed without rotation.
  Controller event sequence numbers and client totals are session-scoped, not restored
  from disk after a controller restart.
- **Role metric:** `raft_node_is_leader` reports the node’s current role, not whether it
  still has a quorum. An isolated leader can briefly show `1` until check-quorum expires;
  use cluster health/leader fields for effective leadership.
- **Snapshot scope:** snapshots are single-payload and uncompressed. A very large state
  may exceed RPC/body limits or increase install time. Chunked snapshots are a documented
  non-goal.
- **Client semantics:** there is no client request ID or deduplication. Retries can put
  duplicate SET/DELETE commands in the log, although the operations are idempotent, so
  the final KV state is the same.
- **Runtime checker:** the consistency checker samples live state and command hashes. It
  found no violations in the observed runs but cannot replace a formal specification or
  model checker. Snapshot history retains only the latest 2,000 applied commands.
- **Frontend:** the production JS bundle is about 853 kB (238 kB gzip). Recharts 2 emits a
  deprecation warning but has no known current vulnerability.
- **Legacy snapshot compatibility:** the preserved populated Docker volume contained
  `raft.db` but no `snapshot.json`. It restarted with all data intact, but a populated
  pre-fix *snapshot file* was not available for an end-to-end migration test. Current
  snapshot-format tests and startup repair are covered.

## Verification results

### Test suites

| Check | Result |
| --- | --- |
| Backend `pytest -q` | **27 passed in 21.50s** (8 core unit, 8 live-process integration, 11 audit regressions) |
| Frontend typecheck | Passed (`tsc --noEmit`) |
| Frontend Vitest | **16 passed** (3 files, 0.68s) |
| Frontend production build | Passed; Vite 8.3.5, 852.80 kB JS / 36.35 kB CSS; gzip 238.41 / 7.31 kB |
| `npm audit` | **0 vulnerabilities** |
| `pip-audit -r requirements.txt` | **0 known vulnerabilities** |
| Final `npm ci` / Docker build | Clean install and image build passed |

Evidence: `docs/audit-evidence/pytest-final.log`,
`docs/audit-evidence/frontend-final.log`, `npm-audit-final.json`,
`pip-audit-final.json`.

### Fault matrix

All ten isolated-cluster scenarios passed (`fault-suite-final.log`):

| Scenario | Key result |
| --- | --- |
| `write_leader_crash` | 345 writes succeeded, 3 failed with “no leader available”; 0 acknowledged keys missing; 0 consistency violations across 17,130 checks; election 0.657s, rejoin 8.808s |
| `crash_after_commit` | Committed value present on the new leader after `SIGKILL` |
| `follower_recovery` | Recovered follower caught up to the committed index |
| `partition_minority_3` | Old leader write `409`; old leader read `409`; new leader committed index 4; no stale read; converged |
| `partition_leader_minority_5` | Minority write rejected; five-node cluster converged |
| `two_nodes_isolated` | Two isolated nodes could not form a competing committed quorum |
| `rapid_leader_failures` | Repeated leader kills converged without losing acknowledged writes |
| `restart_all` | Full process restart recovered and converged |
| `stale_persisted_state` | Restart used fresh durable state, not a stale snapshot |
| `linearizability` | 36 reads checked against committed write order; linearizable |

The checker reported `election_safety`, `leader_append_only`, `log_matching`,
`leader_completeness`, `state_machine_safety` and related rules with zero violations in the
leader-crash run. It is runtime evidence, not a proof.

### Linearizability runs

- **Large concurrent run:** 48 writes, 100 reads, 0 errors, linearizable
  (`linearizability-large-final.log`).
- **Leader-crash run:** 242 writes, 319 reads, 4 read failures during leader loss,
  new leader elected, linearizable (`linearizability-leader-crash-final.log`). The
  failures are fail-closed/no-leader responses; no acknowledged write was lost and no
  stale successful read was observed.

### Read and snapshot reproductions

- `stale-read` before: stale `200`; after: 10/10 `503`, no stale value.
- `read-bugs` before: read ahead of applied (`index=1`, `commit=5`) and read after
  step-down; after: apply-lag timeout and `NotLeaderError`.
- `snapshot-crash` before: applied `(6,1)` with old KV; after: applied `(6,3)` with the
  snapshot KV, history and empty compacted log.
- Regression tests additionally cover gap rejection, duplicate AppendEntries, metadata
  mismatch, corrupt snapshot rebuild, unknown command op, and CORS allow-list behaviour.

### API and deployed stack

- `docker compose build` and `docker compose up -d` recreated controller/dashboard
  successfully without deleting the named volume.
- The existing named volume was reused across the rebuilds rather than deleted. The final
  recreate advanced leader `n1` from term 9 / applied 47 to term 10 / applied 48 after the
  leader no-op; the post-recreate smoke test then committed and read at index 49. See
  `cluster-before-final-recreate.json` / `cluster-after-final-recreate.json` and
  `smoke-final.txt`.
- `/api/health` returned `{"status":"ok","controller_running":true,"cluster_size":3}`.
- Smoke test wrote/read a key through the controller/client and reported all three applied
  indexes at 49 (`read_mode=read-index`) after the final controller rebuild.
- nginx served the dashboard with all security headers; seven routes returned `200`.
- Prometheus `/api/v1/targets` reported both the Prometheus self-target and
  `raftkv-controller` as `up`.
- Loopback-only bindings were verified in `compose-ps-final.txt`. `no-new-privileges` is
  present on controller, dashboard and Prometheus. The stack has no credentials, but KV
  payloads are exposed through events/logs by design (see M-7).
- Final Prometheus names checked include `raft_node_up`,
  `raft_node_rpc_failed`, `raft_node_elections_started`,
  `raft_cluster_committed_entries_per_second`,
  `raft_client_successful_writes_total`, and `raft_network_dropped_messages`.
  A crashed node reported `raft_node_up{node="n2"} 0.0`, and a partition showed failed RPC
  counts on the isolated leader.

### Dashboard interactions

Final build route run: `/`, `/cluster`, `/simulator`, `/kv`, `/timeline`, `/metrics`,
`/reports` all returned `200` with no unexpected console errors
(`ui-routes-final.json`). Browser interaction run
(`ui-interactions-after.json`) verified:

- KV write committed, read returned `read-index`, delete committed.
- Simulator killed and restarted a follower.
- WebSocket offline state was observed and reconnect succeeded.
- A deliberately injected HTTP 500 produced the intended error state; that 500 was the
  only console error.
- Report selection and run-dialog interaction worked.
- Final metric labels and the live-WebSocket indicator rendered in the rebuilt image
  (`ui-labels-final.txt`).
- Delayed API responses showed the KV and Reports loading states, and empty API responses
  showed “The state machine is empty” and “No reports yet”
  (`ui-states-final.txt`).

## Performance

No performance claim is based on a fabricated improvement. The normal-load benchmark is a
fixed-rate workload; the reported rate is a setpoint, not capacity:

| Configured rate | Before: writes/s, p95 | After: writes/s, p95 |
| --- | --- | --- |
| 6 ops/s | 5.10, 25.06 ms | 4.95, 23.98 ms |
| 50 ops/s | 41.98, 23.22 ms | 41.63, 25.91 ms |
| 100 ops/s | 83.32, 23.01 ms | 83.56, 23.78 ms |

The six-scenario reports all remained `passed` with zero consistency violations. The small
differences are run noise; the read fix adds one heartbeat confirmation round and does not
claim a speedup.

**Why `5.39` writes/s is not a capacity result:** it is near the configured low-rate
setpoint. The workload mixes reads and writes, so a 6 ops/s target naturally yields roughly
5 writes/s. The final measurements are 4.95–5.10 writes/s in that mode. Saturating write
load is much higher: ApacheBench against the controller measured **533.8 writes/s at
concurrency 20** and **454.3 writes/s at concurrency 100** in the pre-fix baseline
(`ab-write-c20-baseline.txt`, `ab-write-c100-baseline.txt`); the write path, not the
setpoint, was the limit in those runs.

**About `p95 89.94 ms`:** that exact value is not present in a retained report, so it
cannot be attributed to a specific mode. The retained results show low-rate write p95 of
about 23–26 ms, read-only p95 of **43 ms at concurrency 20** and **87 ms at concurrency
50**, and saturation write p95 of 62 ms (c=20) / 282 ms (c=100). A p95 near 90 ms is
therefore consistent with a concurrent read/load mode, not with the normal low-rate
benchmark. No code change was made to manufacture a better number.

Read-only checks: 714 req/s at c=20 and 731 req/s at c=50, both with zero failed requests
(`ab-read-c20-after.txt`, `ab-read-c50-after.txt`).

## Residual risks and production blockers

1. **No authentication or TLS.** All simulator/KV endpoints are open on the bound
   interface. Loopback defaults are not a security boundary if ports are overridden.
2. **Root controller/dashboard containers.** `no-new-privileges` reduces escalation but
   does not make the containers non-root.
3. **No formal proof.** The checker and fault suite are strong runtime evidence; they do
   not model arbitrary message reordering, disk corruption, clock skew, or every crash
   window.
4. **Unbounded event log file** and session-scoped controller events/counters.
5. **KV observability:** command payloads and keys appear in events, logs and URLs; do not
   use this store for sensitive data.
6. **Single-payload snapshots**; no chunking, compression or cross-version migration suite.
7. **No client deduplication**; retries can duplicate idempotent commands.
8. **No dynamic membership/pre-vote/formal lease**; fixed 3/5-node clusters and
   fresh-quorum reads are deliberate scope limits.
9. **Metrics role gauge is not quorum-aware**; use cluster health for effective leader.
10. **Frontend bundle size** and Recharts 2 deprecation.
11. **Repository state:** this workspace is not a git repository; all fixes are working-tree
    edits. Commit/tag them before publishing.

## Release recommendation

- **Open-source portfolio/demo release:** acceptable after committing the working tree and
  keeping the README caveats. The repaired stale-read and snapshot paths are materially
  safer than the pre-audit version.
- **Interview/demo use:** acceptable with loopback-only Docker, the documented failure
  scenarios, and the retained evidence.
- **Production use on an untrusted network:** not acceptable until authentication/TLS,
  non-root containers, log rotation, and a stronger verification story are added.

## Evidence index

The durable evidence is in [`docs/audit-evidence/`](audit-evidence/). Key files:

- `stale-read-before.log`, `stale-read-final.log`
- `snapshot-crash-before.log`, `snapshot-crash-final.log`
- `read-bugs-before.log`, `read-bugs-final.log`
- `fault-suite-before.log`, `fault-suite-final.log`
- `linearizability-large-final.log`, `linearizability-leader-crash-final.log`
- `pytest-baseline.log`, `pytest-final.log`, `frontend-final.log`
- `npm-audit-before.json`, `npm-audit-final.json`, `pip-audit-final.json`
- `api-security-metrics-after.log`, `metrics-partition-after.txt`, `metrics-final.txt`,
  `prometheus-targets-final.json`, `security-headers-final.txt`
- `ui-routes-final.json`, `ui-interactions-after.json`, `ui-labels-final.txt`,
  `ui-states-final.txt`, `ui-overview.png`, `ui-simulator.png`, `ui-metrics.png`
- `docker-build-final.log`, `docker-up-final.log`, `compose-ps-final.txt`,
  `cluster-before-final-recreate.json`, `cluster-after-final-recreate.json`,
  `smoke-final.txt`
- `perf-summary.json`, `ab-read-c20-after.txt`, `ab-read-c50-after.txt`,
  `ab-write-c20-baseline.txt`, `ab-write-c100-baseline.txt`

Existing benchmark reports remain untouched in [`docs/results/`](results/).
