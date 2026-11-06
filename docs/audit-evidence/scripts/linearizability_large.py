import asyncio, sys, time, json
sys.path.insert(0, '/tmp/raftkv-audit')
from fault_suite import with_cluster, check_linearizable

async def scenario(c, tmp):
    await c.wait_for_leader(15)
    await c.kv_command('set','lin-large','init')
    writes=[]; reads=[]; errors=[]
    async def writer(wid):
        for seq in range(12):
            await asyncio.sleep(0.01 + 0.002*wid)
            start=time.monotonic(); value=f'w{wid}-{seq}'
            try:
                r=await c.kv_command('set','lin-large',value,retries=0)
                writes.append({'index':r['index'],'value':value,'start':start,'end':time.monotonic(),'writer':wid,'seq':seq})
            except Exception as e:
                errors.append(f'writer {wid}: {e}')
    async def reader(rid):
        for seq in range(25):
            await asyncio.sleep(0.005 + 0.0015*rid)
            start=time.monotonic()
            try:
                r=await c.kv_get('lin-large')
                reads.append({'value':r.get('value'),'start':start,'end':time.monotonic(),'reader':rid,'seq':seq})
            except Exception as e:
                errors.append(f'reader {rid}: {e}')
    await asyncio.gather(*(writer(i) for i in range(4)), *(reader(i) for i in range(4)))
    ok, detail=check_linearizable(writes, reads)
    return {'writes':len(writes),'reads':len(reads),'errors':errors[:5],'linearizable':ok,'detail':detail,'error':None if ok and not errors else 'failure'}

r=asyncio.run(with_cluster('linearizability_large',3,scenario))
print(json.dumps(r, indent=2, default=str))
raise SystemExit(0 if r.get('passed') else 1)
