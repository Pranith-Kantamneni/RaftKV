# Implementation Plan

Phases are executed in order; a phase is only complete when its tests pass against real
node processes.

## Phase 1 — Repository inspection & architecture (complete)
- Repository was empty; no reusable code. Greenfield implementation.
- Architecture, component boundaries, data flow, API contracts and risks documented in
  `docs/architecture.md` and this plan.

## Phase 2 — Raft core (complete)
- `LogEntry` / `RaftLog` with snapshot base, term lookup, truncation, batch reads.
- SQLite storage with WAL, `synchronous=FULL`, atomic term/vote and apply transactions.
- KV state machine with applied-index tracking and bounded apply history.
- Raft node: roles, randomized elections, RequestVote rules, heartbeats, per-peer
  replication, consistency checks, current-term commit restriction, no-op on election.
- Unit tests: log behaviour, persistence, vote/up-to-date rules, commit majority math.

**Exit criteria:** unit tests green; three-node in-process simulation elects and replicates.

## Phase 3 — Distributed deployment (complete)
- One FastAPI/uvicorn process per node, argparse entrypoint, `/raft/*` endpoints.
- Controller supervisor spawns processes with dedicated data dirs and ports.
- Mesh proxy process layer; nodes are configured with peer proxy URLs.
- Dockerfiles + compose.

**Exit criteria:** three real processes elect a leader, replicate a write, and respond
committed only after majority apply.

## Phase 4 — Persistence & recovery (complete)
- Restart crashed node, reload term/vote/log/applied state.
- Catch-up through AppendEntries and InstallSnapshot.
- Tests: restart follower, restart leader, snapshot catch-up.

**Exit criteria:** committed data survives restarts; a lagging follower converges.

## Phase 5 — Failure simulator (complete)
- SIGKILL crash, supervised restart, namespace-safe cleanup.
- Proxy partitions (arbitrary groups), per-link latency, per-link loss, seeded random.
- Random failure experiment controller with probability / interval / recovery / limit / seed.

**Exit criteria:** minority cannot commit, majority continues, healing reconciles logs.

## Phase 6 — Monitoring backend (complete)
- Event bus with filtering, metric ring buffer (throughput, percentiles, availability).
- WS broadcast of snapshots; event ingestion by polling + proxy events.
- Prometheus collector at `/metrics`.

## Phase 7 — Frontend (complete)
- Vite/React/TS/Tailwind app shell, WS context with reconnect, typed API client.
- Pages: Overview, Cluster (React Flow), Simulator, KV, Timeline, Metrics, Reports.
- Confirmation dialogs, active-fault banner, responsive layout.
- Vitest tests for client/filters/components.

**Exit criteria:** dashboard renders live state from backend only; controls work.

## Phase 8 — Advanced (complete)
- Snapshot creation, compaction, InstallSnapshot.
- Five-node cluster config (`CLUSTER_SIZE=5`).
- Experiment runner + benchmark comparison + saved reports.

## Phase 9 — Validation (complete)
- Full pytest suite against real processes, frontend tests, production build.
- Run real benchmarks, write results into docs.
- Docker compose smoke test, README and docs final pass.

## Test matrix

| Area | Test |
| --- | --- |
| Log | append/truncate/term-at snapshot boundaries |
| Persistence | term/vote/log survive reopen; applied index atomic |
| State machine | SET/DELETE determinism, applied history |
| Election | 3 nodes elect exactly one leader; term increments |
| Replication | write visible on all 3 nodes after convergence |
| Leader crash | new leader elected; writes continue; old leader rejoins |
| Follower crash | majority continues; restarted follower catches up |
| Partition | minority write fails; majority writes; heal converges |
| Snapshot | compacted follower restored via InstallSnapshot |
| Consistency | checker reports no violations across destructive runs |
| API | HTTP status contracts, input validation |
| Frontend | API client error handling, event filtering, render smoke |

## Deliberate non-goals
- Dynamic membership, client dedup, chunked snapshots, pre-vote, and clock-based leader
  leases. Reads use the fresh-heartbeat ReadIndex check instead of a lease.
  Each is documented in `docs/engineering_decisions.md`.

## Final validation (complete)
- Backend: 27 passed (8 unit, 8 live-process integration, 11 audit regressions), including checker execution during a real leader failure.
- Frontend: 16 passed, typecheck passed, production build passed with zero `npm audit` vulnerabilities.
- Six-scenario benchmark suite passed with zero consistency violations; raw reports in `docs/results/`.
- Docker Compose built and ran: controller and dashboard healthy, Prometheus targets up, follower crash/restart verified, smoke test passed against the deployed stack.
