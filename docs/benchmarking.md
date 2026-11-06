# Benchmarking

## 1. What is measured

There are two layers:

1. **Scenario benchmark** — six Raft/failure scenarios run sequentially through the live
   controller API. Each run uses a client workload while the scenario injects real faults.
2. **Experiment report** — every run stores throughput, client round-trip latency
   percentiles, failures, recovery time, safety-check counts, assertions and an event
   timeline.

All numbers are produced by an actual execution. The scripts display API responses; they do
not synthesize results. Raw reports are stored in `data/experiments/` at runtime and the
published run is committed under [`results/`](results/).

## 2. Published run

**Date:** 2026-10-31  
**Suite ID:** `benchmark-20261031T054524`  
**Outcome:** passed

### Hardware and software

| Item | Value |
| --- | --- |
| CPU | 12th Gen Intel Core i5-1240P |
| Logical CPUs | 16 |
| Memory | 15 GiB |
| OS | Fedora 44, kernel 7.1.3-201.fc44.x86_64 |
| Python | 3.14.6 |
| Storage | SQLite WAL, `synchronous=FULL`, local filesystem |
| Topology | 3 node processes, 3 mesh proxies, controller and client on the same host |

### Configuration

| Parameter | Value |
| --- | --- |
| Duration | 10 s per scenario |
| Attempted operations | 6/s (3 workers at 2/s) |
| Operation mix | 5 writes and 1 read per 6 operations |
| Client retries in workload | 0 (a failed write is counted as failed) |
| Snapshot threshold | 128 applied entries |
| Election timeout | 0.8–1.5 s |
| Heartbeat | 200 ms |
| Latency fault | 200 ms one-way on every inter-node link |
| Loss fault | 25 % of inter-node RPCs |
| Partition | leader vs. both followers |
| Safety sampling | every 500 ms |

### Results

| Scenario | Outcome | Write attempts | Success | Fail | Read attempts | Write ops/s | p50 ms | p95 ms | p99 ms | Recovery | Consistency |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| normal | passed | 52 | 52 | 0 | 9 | 4.95 | 16.12 | 23.77 | 25.65 | — | passed |
| follower_down | passed | 51 | 51 | 0 | 9 | 5.10 | 16.24 | 21.07 | 21.57 | 0.83 s | passed |
| leader_crash | passed | 51 | 45 | 6 | 9 | 4.50 | 16.30 | 22.61 | 27.35 | 0.98 s | passed |
| latency | passed | 52 | 52 | 0 | 9 | 4.95 | 18.09 | 425.26 | 430.05 | — | passed |
| packet_loss | passed | 51 | 51 | 0 | 9 | 5.10 | 20.04 | 221.97 | 222.74 | — | passed |
| partition | passed | 52 | 49 | 3 | 9 | 4.66 | 15.87 | 21.52 | 22.08 | 0.27 s | passed |

`Write ops/s` is successful writes divided by the measured workload window. It is
deliberately not the attempted-operation rate: at a 6 ops/s target with one read in six
operations, the expected write rate is about 5/s.

### Safety evidence

| Scenario | Samples | Checks executed | Violations |
| --- | ---: | ---: | ---: |
| normal | 22 | 16,590 | 0 |
| follower_down | 21 | 15,759 | 0 |
| leader_crash | 21 | 17,777 | 0 |
| latency | 22 | 26,309 | 0 |
| packet_loss | 21 | 27,230 | 0 |
| partition | 22 | 33,129 | 0 |

All seven checks ran in every scenario. Some comparisons were skipped when a sample had
fewer than two reachable nodes (e.g. immediately after leader death); a skip is not counted
as a pass. Every check had `checked > 0` and zero violations by the end of each report.

### Raw reports

- Suite: [`results/benchmark-suite-20261031T054524.json`](results/benchmark-suite-20261031T054524.json)
- Normal: [`results/benchmark-scenario-20261031T054524-e3e52e.json`](results/benchmark-scenario-20261031T054524-e3e52e.json)
- Follower down: [`results/benchmark-scenario-20261031T054536-626358.json`](results/benchmark-scenario-20261031T054536-626358.json)
- Leader crash: [`results/benchmark-scenario-20261031T054547-b7f663.json`](results/benchmark-scenario-20261031T054547-b7f663.json)
- Latency: [`results/benchmark-scenario-20261031T054558-3207bd.json`](results/benchmark-scenario-20261031T054558-3207bd.json)
- Packet loss: [`results/benchmark-scenario-20261031T054610-5299ad.json`](results/benchmark-scenario-20261031T054610-5299ad.json)
- Partition: [`results/benchmark-scenario-20261031T054621-36f98f.json`](results/benchmark-scenario-20261031T054621-36f98f.json)

## 3. How to reproduce

Start the cluster first:

```bash
make run             # or: make docker-up
```

Then run the suite or one scenario:

```bash
# Full suite, with raw JSON output.
.venv/bin/python scripts/benchmark.py --duration 10 --rate 6 \
  --output /tmp/raftkv-benchmark.json

# One scenario.
.venv/bin/python scripts/benchmark.py --scenario partition --duration 10 --rate 6

# Make targets.
make benchmark DURATION=10 RATE=6
make experiment SCENARIO=leader_crash DURATION=10 RATE=6
```

Equivalent API calls:

```bash
curl -X POST http://127.0.0.1:8000/api/benchmarks/run \
  -H 'content-type: application/json' -d '{"duration_s":10,"rate":6}'

curl -X POST http://127.0.0.1:8000/api/experiments/run \
  -H 'content-type: application/json' \
  -d '{"scenario":"leader_crash","duration_s":10,"rate":6}'
```

The dashboard calls the same endpoints from **Metrics → Run benchmark** and **Reports → Run
experiment**.

## 4. Workload and latency definition

For rate `R`:

- `workers = max(1, min(4, int(R // 2) or 1))`;
- `per_worker_rate = R / workers`;
- each worker schedules attempts at a fixed interval;
- every sixth sequence number is a read of a previously written key;
- the rest are `SET` of a unique key `exp:<worker>:<sequence>`;
- writes use `retries=0`, so a failure during an election is visible in the report;
- latency is measured around the full controller → leader → durable majority → local apply
  round trip;
- the final state is read and every acknowledged sample key (up to 200) must be present.

This is an open-loop target, not a closed-loop saturation benchmark. If the cluster is slow,
the worker sleeps less/never and can fall behind its target. Throughput is computed from
successful writes over the real measurement window, not from the requested rate.

## 5. Interpreting the results

- **Normal.** ~5 committed writes/s with a 15–25 ms client round trip. This is fast enough
  to demonstrate correctness; it is not the maximum throughput of the implementation.
- **Follower down.** No write failures and slightly lower p95 than normal. With 2/3 nodes
  alive, quorum is still available.
- **Leader crash.** Six writes failed during the ~0.98 s election window. `recovery_s` is
  time to the new leader, not time for the old leader to rejoin. A new leader was elected in
  a higher term and all acknowledged writes were present after restart.
- **Latency.** 200 ms on every inter-node link produces ~425 ms p95: 200 ms to the follower
  plus 200 ms for the response back, plus normal commit/apply overhead. The workload still
  completed with zero failed writes because 400 ms is below the RPC/client timeouts.
- **Packet loss.** At 25 % loss, each RPC can need retries. p95 ~222 ms and p50 ~20 ms show
  a bimodal distribution: most requests are unaffected, a minority wait for a retry. No
  writes failed in this short run.
- **Partition.** The isolated leader could not commit; the controller discovered the
  majority leader and three writes failed during the transition. After healing, state
  machines converged with no safety violations.

The key result is not the throughput number; it is that every scenario produced zero safety
violations and all acknowledged writes were present after convergence.

## 6. Honest caveats

- **Single host.** Nodes, proxies, controller and client share one CPU and filesystem. This
  measures the protocol and implementation, not network or disk hardware.
- **Short runs.** Ten seconds per scenario and ~50 writes are enough for correctness
  evidence, not for stable percentile tails or capacity planning.
- **Python and SQLite.** Each write performs synchronous durability work. Results reflect a
  Python implementation with `synchronous=FULL`, not a C++/Rust production database.
- **Open-loop target.** A slow cluster can miss its target, and a fast cluster is bounded by
  the configured rate.
- **No warm-up or cooldown.** The first operations include normal initialization.
- **Run-to-run variance.** Election randomization, OS scheduling and proxy timing change
  individual results. Compare medians across repeated runs before drawing conclusions.
- **No client dedup.** A timed-out write is not retried automatically in the benchmark
  workload. In a real client, a retry could append the command again (SET/DELETE are
  idempotent at the state-machine level).
- **Recovery semantics differ.** Follower recovery is time from restart to convergence;
  leader recovery is time from crash to new leader; partition recovery is time from heal to
  convergence.

## 7. What would make a stronger benchmark

- Longer runs (minutes), warm-up, and repeated trials with median/IQR.
- Multi-host deployment with real network latency and separate disks.
- Closed-loop load with a fixed concurrency and a latency SLA.
- Varying key/value sizes and read/write mixes.
- CPU, memory, fsync and network counters sampled alongside client latency.
- Comparing against a reference implementation under the same workload.

These are future work, not claims about the current numbers.
