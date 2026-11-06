# Failure Scenarios

Every fault in this project affects a real process or real HTTP traffic. No fault is
represented only in frontend state. This document explains the mechanisms, expected
behaviour, how to run each scenario, and what the automated experiment assertions require.

## 1. Fault layer

### Process faults

The controller's supervisor owns three node `subprocess.Popen` processes, each in its own
session (`start_new_session=True`). A crash is `SIGKILL` to the whole process group:

```text
os.killpg(os.getpgid(pid), SIGKILL)
```

There is no graceful shutdown notification, no final fsync and no in-memory flush. The node
must recover from whatever is already durable. Restart spawns a new Python process with the
same data directory, so term/vote, log, KV state and snapshot are reloaded from disk.

### Network faults

Each node has a mesh proxy (ports 7101–7103). All inter-node Raft RPCs are sent to the
**target proxy**, not the target node directly. The proxy:

1. Reads `X-Raft-From` to identify the source node.
2. Applies partition rules for the directed `(source, target)` pair.
3. Applies packet loss using a seeded RNG.
4. Sleeps for configured one-way latency.
5. Forwards over a real HTTP socket to the target node.

Management traffic from the controller goes directly to node ports (7001–7003), so an
isolated node remains inspectable without breaching the partition.

### What is not simulated

- No disk corruption, torn writes or storage latency injection.
- No clock skew or kernel panic.
- No CPU starvation or memory pressure.
- No byzantine nodes.
- Pros: faults hit the protocol. Con: this is not a complete chaos-engineering platform.

## 2. Manual fault controls

### Crash a node

UI: **Failure simulator → Node processes → Crash**.

```bash
curl -X POST http://127.0.0.1:8000/api/nodes/n2/crash
```

Expected:

- process is `SIGKILL`ed and immediately unreachable;
- cluster health becomes `degraded` unless the crashed node was needed for quorum;
- if a follower died, the leader and second follower continue to commit;
- if the leader died, a majority elects a new leader in a higher term;
- writes attempted during the election window fail after retries;
- no acknowledged write is lost.

### Restart a node

```bash
curl -X POST http://127.0.0.1:8000/api/nodes/n2/restart
```

Expected:

- a new PID appears;
- the node starts as follower with persisted term/vote, log and applied state;
- the leader catches it up through AppendEntries, or InstallSnapshot if needed;
- applied indexes converge and state machines agree.

### Crash the current leader

```bash
curl -X POST http://127.0.0.1:8000/api/simulation/leader-failure
```

This resolves the current leader first, then crashes it. The response names the crashed
node and returns the post-crash cluster snapshot.

### Partition arbitrary groups

The groups must cover every node exactly once and contain at least two groups.

```bash
# Isolate the leader from the other two nodes.
curl -X POST http://127.0.0.1:8000/api/simulation/partition \
  -H 'content-type: application/json' \
  -d '{"groups":[["n1"],["n2","n3"]]}'

# Heal.
curl -X POST http://127.0.0.1:8000/api/simulation/heal
```

Expected with a 3-node cluster and `{leader}` isolated:

- the leader can still see its own log but cannot contact either follower;
- its writes eventually fail (commit timeout; HTTP `504` to a direct client);
- its lease-style reads fail the fresh-quorum test after the freshness window;
- check-quorum makes it step down, so it stops claiming leadership;
- the two-node majority elects a new leader and continues to serve writes;
- a direct write to the isolated node returns `409`, `503` or `504`;
- after heal, the old node truncates any uncommitted suffix and adopts majority history.

Expected with `{n1,n2}` and `{n3}`:

- the two-node side has quorum and may retain/elect leadership;
- `n3` cannot commit or serve a successful leader read;
- healing causes `n3` to catch up.

The partition test uses `{leader}` vs. the rest, which is the destructive minority case.

### Latency

```bash
# 200 ms one-way on every inter-node link.
curl -X POST http://127.0.0.1:8000/api/simulation/latency \
  -H 'content-type: application/json' -d '{"latency_ms":200}'

# 250 ms only from n1 to n2.
curl -X POST http://127.0.0.1:8000/api/simulation/latency \
  -H 'content-type: application/json' \
  -d '{"latency_ms":250,"source":"n1","target":"n2"}'
```

Latency is applied on the target proxy after the request arrives, so the added delay is
one-way per RPC. A leader-to-follower AppendEntries and the response each traverse their own
proxy path: delaying every link twice can add roughly 400 ms to a commit round trip plus
application overhead.

If latency approaches/ exceeds the election timeout, expect election churn and reduced
throughput. That is real Raft behaviour, not a bug.

### Packet loss

```bash
# Drop 25% of inter-node requests on every directed link.
curl -X POST http://127.0.0.1:8000/api/simulation/packet-loss \
  -H 'content-type: application/json' -d '{"loss":0.25}'

# Drop 10% only from n2 to n3.
curl -X POST http://127.0.0.1:8000/api/simulation/packet-loss \
  -H 'content-type: application/json' \
  -d '{"loss":0.1,"source":"n2","target":"n3"}'
```

A dropped request returns a simulated `503` with `X-Sim-Dropped`; the Raft RPC client treats
it as a failed RPC and retries. Loss is evaluated independently per request using a seeded
RNG, so a run is reproducible given the same request sequence.

### Seeded random failures

```bash
curl -X POST http://127.0.0.1:8000/api/simulation/random-failures \
  -H 'content-type: application/json' \
  -d '{
    "probability": 0.35,
    "interval_s": 3,
    "recovery_delay_s": 4,
    "max_failures": 2,
    "seed": 1234,
    "duration_s": 30
  }'
```

Every `interval_s`, the controller inspects live nodes and kills each with probability
`probability`, up to `max_failures`. Each crashed node gets an independent delayed restart.
The seed makes the crash selection reproducible for the same sequence of live-node sets.
The controller does not exempt the leader. A random run can therefore take the cluster below
quorum; write availability is expected to suffer.

Stop and restart all nodes crashed by the experiment:

```bash
curl -X POST http://127.0.0.1:8000/api/simulation/stop
```

Clear network faults and restart random-experiment victims:

```bash
curl -X POST http://127.0.0.1:8000/api/simulation/reset
```

## 3. Automated experiment scenarios

`backend/simulator/experiments.py` runs a client workload and injects faults at about 30 %
of the requested duration, removes them at about 65 %, and then waits for convergence.

| Scenario | Injection | Removed | Key assertions |
| --- | --- | --- | --- |
| `normal` | none | — | no write failures, consistency, convergence, acknowledged keys present |
| `follower_down` | SIGKILL one follower | restarted at 65 % | recovery completed, safety, convergence |
| `leader_crash` | SIGKILL current leader | old leader restarted at 80 % | new leader elected, election < 15 s, safety, convergence |
| `latency` | 200 ms one-way on all links | 0 ms at 65 % | cluster remained available, safety, convergence |
| `packet_loss` | 25 % loss on all links | 0 % at 65 % | cluster remained available, safety, convergence |
| `partition` | isolate leader from followers | heal at 65 % | minority direct write rejected, majority elected, safety, convergence |

All scenarios additionally assert:

- `raft_safety_properties`: the live consistency checker observed no violation;
- `state_machines_converged`: all reachable nodes have identical KV maps;
- `acknowledged_writes_persisted`: every acknowledged sample key is present at the end;
- `leader_available_at_end`: a leader exists after convergence.

The workload uses unique keys, about one read per six operations, and records latency around
the full controller-to-leader-to-majority-to-apply round trip. Failures during the election
window are counted, not hidden with infinite retries (`retries=0` in the experiment
workload).

### Run one scenario

```bash
.venv/bin/python scripts/benchmark.py --scenario leader_crash --duration 10 --rate 6
```

Or from **Reports → Run experiment**.

### Run the full suite

```bash
make benchmark DURATION=10 RATE=6
```

The UI runs the same `/api/benchmarks/run` endpoint and shows each scenario, assertions,
safety-check counts, and the event timeline.

## 4. What to watch

- **Overview:** leader, term, quorum, health, active faults, recent events.
- **Live cluster:** process cards, current term/log/commit/applied indexes, RPC edges.
- **Simulator:** active partitions/links, per-node process state, crash/restart actions.
- **Timeline:** `election_started`, `leader_elected`, `leader_lease_expired` (the event
  name is retained for compatibility; it means check-quorum expired), `log_truncated`,
  `snapshot_installed`, network drops, client failures, assertion events.
- **Metrics:** throughput and latency history, network RPC latency, active nodes.
- **Reports:** safety-check counts, consistency violations, assertions, convergence,
  final leader/term, raw timeline.

## 5. Recovery expectations and caveats

- Election time is usually under ~1.5 s with default settings (0.8–1.5 s timeouts), but can
  be longer under injected latency or loss.
- A leader crash necessarily causes short write unavailability. Six failed writes in a
  10 s run at 6 attempted ops/s is a plausible and honest result.
- The old leader is restarted after a new leader is elected in `leader_crash`; `recovery_s`
  measures time to new leader, `rejoin_s` measures old-leader catch-up.
- A restarted node can require a snapshot if it missed more than the snapshot threshold of
  applied entries while down.
- If all nodes are crashed by a random experiment, no client traffic succeeds until enough
  processes are restarted. The controller does not vote, so it cannot rescue the cluster.
- Partitions are enforced at the proxy layer. Controller management requests bypass the
  partition; direct client requests to an isolated node are still possible and are rejected
  by that node's own role/quorum checks.
- Stopping the controller gracefully stops all node processes. Killing the controller with
  `SIGKILL` can leave orphan node processes; the supervisor starts sessions, so clean up
  manually in that case.

## 6. Reproducing the failure tests

The destructive integration tests use the same mechanisms:

```bash
.venv/bin/python -m pytest backend/tests/test_cluster_integration.py -q
```

They cover leader failover and rejoin, partition minority rejection and heal, restart
persistence, consistency checking during leader failure, a five-node cluster, and snapshot
catch-up. See [`testing.md`](testing.md) for exact output and what each test proves.
