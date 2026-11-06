import asyncio, tempfile, time
from backend.config import RaftSettings
from backend.raft.models import LogEntry
from backend.raft.node import NodeConfig, RaftNode, NotLeaderError, QuorumUnavailableError

async def main():
    tmp = tempfile.mkdtemp(prefix="raftkv-read-after-")
    settings = RaftSettings(election_timeout_min=0.4, election_timeout_max=0.8,
                            heartbeat_interval=0.1, rpc_timeout=0.3,
                            snapshot_threshold=1000, client_timeout=3)
    node = RaftNode(NodeConfig("n1", 9999, {"n2":"http://127.0.0.1:1","n3":"http://127.0.0.1:2"}, tmp, settings, seed=1))
    try:
        entry = LogEntry(1, 1, "command", {"op":"set","key":"k","value":"old"})
        node.storage.append_entry(entry); node.log.append(entry)
        node.state_machine.apply(entry)
        node.role = "leader"; node.current_term = 1
        node.commit_index = 5
        node.last_ack["n2"] = time.monotonic()
        async def confirm(*, since=None):
            return True
        node._confirm_quorum = confirm
        started = time.monotonic()
        try:
            await node.read_key("k")
            print("READ_WITH_LAGGING_APPLY unexpectedly succeeded")
        except QuorumUnavailableError as exc:
            print("READ_WITH_LAGGING_APPLY rejected in", round(time.monotonic()-started,3), "s:", exc)

        async def slow_confirm(*, since=None):
            await asyncio.sleep(0.1)
            return True
        node._confirm_quorum = slow_confirm
        node.commit_index = 1
        node.role = "leader"; node.current_term = 2
        async def step_down_soon():
            await asyncio.sleep(0.05)
            node.role = "follower"; node.current_term = 3
        task = asyncio.create_task(node.read_key("k"))
        await step_down_soon()
        try:
            await task
            print("READ_AFTER_STEPDOWN unexpectedly succeeded")
        except NotLeaderError:
            print("READ_AFTER_STEPDOWN rejected with NotLeaderError")
    finally:
        await node._rpc.close(); node.storage.close()

asyncio.run(main())
