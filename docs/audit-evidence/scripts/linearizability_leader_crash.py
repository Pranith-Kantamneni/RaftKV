import asyncio, sys, time, json
sys.path.insert(0, '/tmp/raftkv-audit')
from fault_suite import with_cluster, check_linearizable

async def scenario(c, tmp):
    leader = await c.wait_for_leader(15)
    assert leader
    await c.kv_command('set','lin-crash','init')
    writes=[]; reads=[]; errors=[]; read_failures=0
    stop=asyncio.Event()
    async def writer(wid):
        seq=0
        while not stop.is_set():
            seq+=1; value=f'w{wid}-{seq}'; start=time.monotonic()
            try:
                r=await c.kv_command('set','lin-crash',value,retries=0)
                writes.append({'index':r['index'],'value':value,'start':start,'end':time.monotonic(),'writer':wid,'seq':seq})
            except Exception as e:
                if len(errors)<5: errors.append(f'writer {wid}: {e}')
            await asyncio.sleep(0.035)
    async def reader(rid):
        nonlocal read_failures
        seq=0
        while not stop.is_set():
            seq+=1; start=time.monotonic()
            try:
                r=await c.kv_get('lin-crash')
                reads.append({'value':r.get('value'),'start':start,'end':time.monotonic(),'reader':rid,'seq':seq})
            except Exception:
                read_failures+=1
            await asyncio.sleep(0.02)
    tasks=[asyncio.create_task(writer(i)) for i in range(2)] + [asyncio.create_task(reader(i)) for i in range(2)]
    await asyncio.sleep(1.2)
    crashed=time.monotonic()
    await c.crash_node(leader, source='audit')
    new_leader=await c.wait_for_leader(15)
    await asyncio.sleep(2.0)
    await c.restart_node(leader)
    await asyncio.sleep(2.0)
    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    ok, detail=check_linearizable(writes, reads)
    return {'old_leader':leader,'new_leader':new_leader,'writes':len(writes),'reads':len(reads),
            'read_failures':read_failures,'errors':errors,'linearizable':ok,'detail':detail,
            'error':None if ok else 'linearizability failure'}

r=asyncio.run(with_cluster('linearizability_leader_crash',3,scenario))
print(json.dumps(r, indent=2, default=str))
raise SystemExit(0 if r.get('passed') else 1)
