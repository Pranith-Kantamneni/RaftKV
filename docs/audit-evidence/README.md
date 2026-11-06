# Audit evidence index

These are the retained artifacts from the 2026-11-01 release audit. They are copies of
scratch files created under `/tmp/raftkv-audit-backup`; the original Docker volume and the
existing benchmark reports in `docs/results/` were not modified.

| File | What it records |
| --- | --- |
| `pytest-baseline.log` | Pre-fix backend suite: 16 passed |
| `pytest-final.log` | Final backend suite: 27 passed |
| `frontend-final.log` | Final frontend typecheck, Vitest and production build |
| `fault-suite-before.log` | Fault matrix before the audit fixes |
| `fault-suite-final.log` | Final 10-scenario fault matrix, all passed |
| `linearizability-large-final.log` | 48 writes / 100 reads, linearizable |
| `linearizability-leader-crash-final.log` | 242 writes / 319 reads across a leader crash, linearizable |
| `stale-read-before.log` | Old leader returned `200` with the old value after a new leader committed |
| `stale-read-final.log` | Final read path rejected 10/10 stale reads with `503` |
| `snapshot-crash-before.log` | Interrupted InstallSnapshot left applied index ahead of KV state |
| `snapshot-crash-final.log` | Startup reconciliation restored the snapshot state atomically |
| `read-bugs-before.log` | Read-ahead-of-apply and read-after-stepdown failures |
| `read-bugs-final.log` | Both read failures now fail closed |
| `security-before.txt` | Wildcard CORS and missing long-key validation before the fixes |
| `security-headers-final.txt` | Final nginx response headers |
| `api-security-metrics-after.log` | CORS/validation/metrics checks and crashed-node metric after the fixes |
| `metrics-partition-after.txt` | Prometheus output during a leader-minority partition |
| `metrics-final.txt` | Final Prometheus exposition after the rebuilt stack was deployed |
| `prometheus-targets-final.json` | Final Prometheus scrape-target health |
| `npm-audit-before.json` | Frontend audit before upgrades: 8 advisories (2 critical, 1 high, 5 moderate) |
| `npm-audit-final.json` | Frontend audit after upgrades: 0 advisories |
| `pip-audit-final.json`, `pip-audit-final.err` | Python dependency audit: 0 known vulnerabilities |
| `perf-summary.json` | Selected fields from the six normal-load benchmark reports |
| `ab-read-c20-after.txt`, `ab-read-c50-after.txt` | Read-only load with 20 and 50 concurrent clients |
| `ui-routes-before.json` | Route smoke test before the final rebuild |
| `ui-routes-final.json` | Seven routes, final build, no unexpected console errors |
| `ui-interactions-after.json` | Browser write/read/delete, kill/restart, WebSocket reconnect, error state, reports |
| `ui-overview.png`, `ui-simulator.png`, `ui-metrics.png` | Final dashboard screenshots |
| `docker-build-final.log`, `docker-up-final.log` | Final image build and recreate |
| `cluster-before-final-recreate.json`, `cluster-after-final-recreate.json` | Existing-volume state before and after the final recreate |
| `smoke-final.txt` | Final post-recreate controller smoke test at applied index 49 |
| `compose-ps-final.txt` | Final published ports and container health |
| `ui-labels-final.txt` | Final browser check that the corrected metric labels and live WebSocket indicator render |
| `ui-states-final.txt` | Delayed-API loading states and empty states for KV and Reports |
| `ab-write-c20-baseline.txt`, `ab-write-c100-baseline.txt` | Pre-fix write saturation at concurrency 20 and 100 |

`ui-metrics.png` was captured after the final rebuild, so it shows the corrected
"Commit rate" / "Successful writes" labels and the live-WebSocket indicator.

The full benchmark JSON files remain in `docs/results/`. The scratch copies under
`/tmp/raftkv-audit-backup` are disposable; this directory is the durable copy.

The raw audit scripts are kept in `scripts/` for reproducibility. Run them from the
repository root with `PYTHONPATH=.` and the project virtualenv; they create isolated
clusters/data directories and do not touch the Docker volume. `fault_suite.py` is the full
10-scenario matrix; `stale_read.py`, `snapshot_crash.py`, `read_bugs.py` /
`read_bugs_after.py` are the focused pre/post reproductions; `ui_*.py` are the browser
checks. `read_bugs.py` is the pre-fix harness; `read_bugs_after.py` is the post-fix rerun.
