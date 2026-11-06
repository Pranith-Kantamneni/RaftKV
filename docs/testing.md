# Testing

## Philosophy

The backend integration tests do not mock Raft or the network. Every integration fixture
starts three or five real node processes on ephemeral ports, starts one mesh proxy per node,
and talks to them over HTTP. Tests kill processes with `SIGKILL`, partition real proxies,
restart nodes, and inspect the resulting durable state.

The database and state machine unit tests use temporary directories but real SQLite files.
The only mocked layer is the browser `fetch` in the frontend API client tests.

The consistency checker is **runtime evidence, not a formal proof**. It samples live logs
and state machines and can miss a transient violation between samples. The deterministic
unit tests and destructive integration tests cover the protocol rules; the checker proves
that those rules held in the observed runs and gives each report auditable counts.

## Commands

```bash
# Everything
make test
make typecheck
make build

# Backend unit + integration
.venv/bin/python -m pytest -q

# One destructive integration test
.venv/bin/python -m pytest \
  backend/tests/test_cluster_integration.py::test_consistency_checker_during_leader_failure -q

# Frontend
cd frontend && npm test

# Docker smoke (requires a running stack)
.venv/bin/python scripts/smoke_test.py
```

`pytest` is configured with `asyncio_mode = "auto"` in `pyproject.toml`.

## Last verified results

Environment: Fedora 44, kernel 7.1.3, Python 3.14.6, Node 22.23.2, Intel i5-1240P.

```text
$ .venv/bin/python -m pytest -q
..........................                                               [100%]
27 passed in 21.50s

$ cd frontend && npm test
✓ src/utils/format.test.ts (5 tests)
✓ src/services/api.test.ts (7 tests)
✓ src/components/components.test.tsx (4 tests)
Test Files  3 passed (3)
Tests       16 passed (16)

$ npm run typecheck
passed

$ npm run build
vite v8.3.5 building client environment for production...
✓ 2294 modules transformed
dist/assets/index-NY_nYGMX.css   36.35 kB │ gzip:   7.31 kB
dist/assets/index-BTEljvBg.js   852.80 kB │ gzip: 238.41 kB
✓ built in 1.90s

$ npm audit
found 0 vulnerabilities
```

The Python dependency tree was checked separately with `pip-audit` (installed ad hoc, not
a project dependency): `pip-audit -r requirements.txt` reported
`No known vulnerabilities found`. The complete audit record is in
[`docs/audit.md`](audit.md).

All 27 backend tests are collected below. Eight are deterministic unit tests, eight are
live-process integration tests, and eleven are audit regression tests.

## Backend unit tests (`backend/tests/test_core.py`)

| Test | What it proves |
| --- | --- |
| `test_log_term_lookup_and_truncation` | Snapshot base, last index/term, term lookup, suffix truncation, compacted-boundary rejection |
| `test_log_requires_contiguous_entries` | A Raft log cannot start with or append a gap |
| `test_storage_persists_term_vote_log_and_kv` | Term/vote/log/KV/applied history survive close and reopen |
| `test_state_machine_rejects_non_contiguous_apply` | Apply is strictly in index order; delete is deterministic |
| `test_snapshot_compaction_survives_restart` | Snapshot file + DB reconciliation after compaction and restart |
| `test_vote_rules_and_persistence` | One vote per term, stale-term rejection, idempotent regrant, persisted term/vote, AppendEntries higher-term handling |
| `test_append_entries_conflict_truncates_suffix` | Conflicting suffix is truncated and replaced; committed prefix remains |
| `test_leader_commit_requires_current_term` | A majority match on old-term entries does **not** commit without a current-term entry |

## Backend audit regression tests (`backend/tests/test_audit_regressions.py`)

| Test | What it proves |
| --- | --- |
| `test_interrupted_install_snapshot_restores_state_and_applied_index` | A crash after the InstallSnapshot file is durable but before the database commit restores KV, history and applied metadata together |
| `test_corrupt_snapshot_file_is_rebuilt_from_durable_applied_state` | A corrupt snapshot file is regenerated from the durable KV/applied prefix instead of being served |
| `test_read_confirm_rejects_acknowledgements_older_than_the_window` | A read cannot reuse heartbeats sent before the read began |
| `test_read_fails_instead_of_returning_ahead_of_applied_state` | A read returns `503` rather than serving a value behind the confirmed commit index |
| `test_read_rechecks_leader_after_waiting_for_quorum` | A read that loses leadership during quorum confirmation returns `409`, not follower state as a successful read |
| `test_append_entries_never_acknowledges_a_gap` | A non-contiguous AppendEntries batch is not acknowledged past the missing entry |
| `test_install_snapshot_rejects_metadata_mismatch` | A snapshot whose payload metadata disagrees with the RPC is rejected without touching state |
| `test_duplicate_append_entries_is_idempotent` | Replaying the same AppendEntries request does not duplicate the log entry |
| `test_append_entries_rejects_unknown_command_op` | An invalid state-machine command is rejected before it can become committed and wedge the apply loop |
| `test_cors_is_opt_in_and_never_wildcard` | CORS is absent by default, allows only configured origins, and never emits a wildcard |
| `test_cors_rejects_wildcard_at_startup` | `CORS_ORIGINS=*` fails fast instead of restoring wildcard CORS |

## Backend integration tests (`backend/tests/test_cluster_integration.py`)

Each test uses real subprocesses and proxies. Ports and data directories are unique per
test.

| Test | Real fault | Assertions |
| --- | --- | --- |
| `test_election_and_replication` | none | one leader, write committed, value and applied index on all three nodes, identical applied command hashes |
| `test_leader_crash_failover_and_rejoin` | `SIGKILL` leader | new leader in higher term, new write commits, old leader restarts, catches up and is follower |
| `test_partition_minority_cannot_commit` | `{leader}` vs `{two followers}` proxies | isolated leader steps down, direct write returns 409/503/504, majority elects and commits, heal converges, no forbidden value |
| `test_persistence_across_restart` | graceful restart of follower | KV and applied index survive, term does not regress |
| `test_consistency_checker_on_live_cluster` | none; checker sampling | all seven checks execute, no violations, final convergence |
| `test_consistency_checker_during_leader_failure` | `SIGKILL` leader while sampling | all seven checks execute across failover/restart, no violations, old leader catches up, convergence |
| `test_five_node_cluster` | `SIGKILL` leader in 5-node cluster | 4/5 quorum elects replacement; writes commit after failover |
| `test_snapshot_catchup` | follower down while 14 writes occur with threshold 5 | leader snapshots, follower returns, catches up all keys via snapshot/log |

### Why `test_consistency_checker_during_leader_failure` matters

The test starts a 100 ms sampler, commits a key, records the client acknowledgment, kills the
current leader, waits for a replacement, commits another key, restarts the old leader, waits
for catch-up, and only then stops the sampler. It asserts:

- final state machines converged;
- the checker report passed;
- at least five samples were taken;
- each of the seven checks has `checked > 0`, zero violations and `passed: true`.

Run it alone (real result from this workspace):

```text
$ .venv/bin/python -m pytest \
    backend/tests/test_cluster_integration.py::test_consistency_checker_during_leader_failure -q
.                                                                        [100%]
1 passed in 4.44s
```

## Consistency checker

Implemented in `backend/simulator/consistency.py`. It polls `/raft/status`, `/raft/log` and
`/raft/debug` from reachable nodes during experiments.

| Check | Evidence collected |
| --- | --- |
| `election_safety` | Maps each observed term to exactly one leader |
| `leader_append_only` | Remembers each `(leader, term)` log; later samples may not change or remove existing entries |
| `log_matching` | For equal `(index, term)` across nodes, commands and immediate prefix terms must match |
| `leader_completeness` | Tracks entries observed committed anywhere; later leaders must contain them unless covered by snapshot |
| `state_machine_safety` | Same applied index must have the same command hash on every node |
| `commit_safety` | Commit is monotonic per node while uptime increases; applied <= commit; commit <= last log (outside snapshot) |
| `client_ack_safety` | Successful client indexes must be applied on the acknowledging leader; waits 0.75 s to avoid judging a stale status sample |

A check is only marked passed when `checked > 0` and `violations == 0`. Skips are shown in
reports. When a node is unreachable, comparisons that need two nodes are skipped rather than
treated as success. Leader-completeness skips committed entries from terms newer than a
leader's term, which is the legitimate partition case (an old leader cannot know the future).

### Live benchmark safety evidence

The six-scenario suite in [`benchmarking.md`](benchmarking.md) ran the checker during every
scenario. All reports passed, with no violations:

| Scenario | Samples | Total checks executed | Violations |
| --- | ---: | ---: | ---: |
| normal | 22 | 16,590 | 0 |
| follower_down | 21 | 15,759 | 0 |
| leader_crash | 21 | 17,777 | 0 |
| latency | 22 | 26,309 | 0 |
| packet_loss | 21 | 27,230 | 0 |
| partition | 22 | 33,129 | 0 |

Totals here are the checker's `total_checks_executed`; they count individual comparisons,
not independent proof obligations. `leader_crash` still executed every check (with some
skips while only one node was reachable). Raw reports are in [`results/`](results/).

## Frontend tests

`frontend/src/services/api.test.ts` (7 tests):

- typed cluster response and request construction;
- JSON write serialization;
- URL encoding for keys containing `/`, spaces, `?` and `&`;
- directed latency payload;
- server error details become `ApiError` with status;
- transport failure becomes status 0;
- plain-text error fallback.

`frontend/src/components/components.test.tsx` (4 tests):

- confirmation dialog invokes confirm only after user action;
- cancel closes, Escape closes, busy dialog ignores Escape;
- event row expands and shows structured payload;
- empty event list message.

`frontend/src/utils/format.test.ts` (5 tests):

- number/missing-value formatting;
- millisecond and duration formatting;
- binary byte units;
- severity ranking used by Timeline filtering;
- percentages and relative time.

These tests do not drive a browser. The dashboard was additionally checked manually with
headless Chrome against the production nginx container:

- all seven deep links (`/`, `/cluster`, `/simulator`, `/kv`, `/timeline`, `/metrics`,
  `/reports`) returned 200;
- `/cluster` rendered the React Flow topology with live node data and RPC edges;
- the nginx `/api` proxy returned the controller health response;
- a screenshot is in [`images/cluster.png`](images/cluster.png).

## Docker verification

Performed on 2026-10-31 and repeated on 2026-11-01 with Docker 29.6.0 and Compose 5.4.0:

1. `docker compose build` built both images.
2. `docker compose up -d` started controller (healthy), dashboard (healthy), Prometheus
   (healthy after correcting the SELinux bind-mount label with `:ro,Z`).
3. Prometheus reported both scrape targets `up`; `raft_cluster_has_leader` returned `1`.
4. The smoke test committed and read a key and confirmed applied indexes `n1=n2=n3`.
5. A follower was crashed through the public API; the cluster reported `degraded` and the
   node `crashed`; restart restored `healthy`, and the smoke test passed again.
6. All seven dashboard deep links returned 200 through nginx.
7. The final rebuild retained the existing named volume; the cluster restarted at term 10
   with all prior applied state and the smoke test passed at index 49.
8. A headless-browser run wrote, read and deleted a key through the UI, killed and
   restarted a follower through the simulator, forced a WebSocket close and observed
   reconnect, and verified the injected API-error state. See `docs/audit.md`.
9. Delayed API responses showed the KV/Reports loading states; empty responses showed
   “The state machine is empty” and “No reports yet”.

`docker compose ps` after verification:

```text
controller running healthy
dashboard  running healthy
prometheus running
```

## Experiments as tests

The experiment runner is a complement to pytest, not a replacement. It asserts:

- no checker violations;
- all reachable state machines converge;
- every acknowledged sample key is present;
- a leader exists at the end;
- scenario-specific conditions (minority rejection, new leader, availability under faults).

`outcome: "passed"` requires every assertion. Reports are never edited by hand. The raw
suite and per-scenario reports for the published benchmark are committed under
`docs/results/`.

## What is not covered

- No automated browser E2E suite; the API client and components are unit-tested and the
  deployed pages were smoke-checked with headless Chrome.
- No formal TLA+/model-checking proof of Raft.
- No disk-corruption, fsync-loss or clock-skew tests.
- No multi-host network tests; all nodes, proxies and clients run on one host.
- No sustained soak test beyond the published short benchmark runs.
- No Windows support (process-group `SIGKILL` relies on POSIX).

## Adding tests

- Unit-level protocol changes belong in `backend/tests/test_core.py`.
- Anything involving processes, proxies, timing or recovery belongs in
  `backend/tests/test_cluster_integration.py` and must use the `cluster` fixture or a unique
  port/data directory.
- A bug fix should add a regression test and, where relevant, a new consistency assertion or
  experiment assertion. Do not remove a check to make a report pass.
