"""Best-effort ephemeral presence. Only activity() renews the 60s viewer lease.

Sorted set scores are Redis server timestamps of USER activity, never delivery times.
A 30s cleanup grace allows the worker to remove expired counts from attacker screens.
All operations have a short budget: Redis failure cannot block gameplay indefinitely.
"""
import asyncio
import json
from uuid import UUID

TTL = 60
RETENTION = 90
INDEX = 'challenge:spectators:index'
SCRIPT = r'''
local t=redis.call('TIME')
local now=tonumber(t[1])+tonumber(t[2])/1000000
local op=ARGV[1]
local mid=ARGV[2]
local uid=ARGV[3]
local ttl=tonumber(ARGV[4])
local retention=tonumber(ARGV[5])
local stale=redis.call('ZRANGEBYSCORE',KEYS[1],'-inf',now-ttl)
for _,v in ipairs(stale) do redis.call('HDEL',KEYS[2],v) end
redis.call('ZREMRANGEBYSCORE',KEYS[1],'-inf',now-ttl)
if op=='activity' then
 redis.call('ZREMRANGEBYSCORE',KEYS[3],'-inf',now-retention)
 redis.call('ZADD',KEYS[1],now,uid)
 redis.call('HSET',KEYS[2],uid,ARGV[6])
 redis.call('EXPIRE',KEYS[1],retention)
 redis.call('EXPIRE',KEYS[2],retention)
 redis.call('ZADD',KEYS[3],now,mid)
 redis.call('EXPIRE',KEYS[3],retention)
elseif op=='leave' then
 redis.call('ZREM',KEYS[1],uid)
 redis.call('HDEL',KEYS[2],uid)
elseif op=='reference' then
 -- CAS: a delayed delivery cannot resurrect a stopped/reopened session.
 if redis.call('HGET',KEYS[2],uid)==ARGV[6] then
  redis.call('HSET',KEYS[2],uid,ARGV[7])
 end
elseif op=='clear' or (op=='clear_empty' and redis.call('ZCARD',KEYS[1])==0) then
 redis.call('DEL',KEYS[1],KEYS[2])
 redis.call('ZREM',KEYS[3],mid)
end
return redis.call('HGETALL',KEYS[2])
'''
BATCH = r'''
local now=tonumber(redis.call('TIME')[1])
redis.call('ZREMRANGEBYSCORE',KEYS[1],'-inf',now-tonumber(ARGV[1]))
return redis.call('ZRANGE',KEYS[1],tonumber(ARGV[2]),tonumber(ARGV[2])+49)
'''


class Presence:
    def __init__(self, redis):
        self.redis = redis
        self.offset = 0

    @staticmethod
    def keys(mid):
        mid = str(UUID(mid))
        return [f'challenge:spectators:{mid}', f'challenge:spectator_refs:{mid}', INDEX]

    async def _op(self, op, mid, uid=0, value='', replacement=''):
        try:
            async with asyncio.timeout(0.4):
                raw = await self.redis.eval(SCRIPT, 3, *self.keys(mid), op, str(UUID(mid)),
                                            str(uid), TTL, RETENTION, value, replacement)
            def decode(v):
                return v.decode() if isinstance(v, bytes) else v
            return {int(decode(raw[i])): decode(raw[i+1]) for i in range(0,len(raw),2)}
        except Exception:
            return None

    async def activity(self, mid, uid, message_id, page=0):
        # Caller already passed SQL authorization + flag and in_progress checks.
        from uuid import uuid4
        ref = json.dumps({'message_id': message_id, 'page': page, 'token': uuid4().hex})
        rows = await self._op('activity', mid, uid, ref)
        return ref if rows is not None else None

    async def viewers(self, mid):
        return await self._op('read', mid)

    async def count(self, mid):
        rows = await self.viewers(mid)
        return len(rows) if rows is not None else None

    async def reference(self, mid, uid, old, new):
        return await self._op('reference', mid, uid, old, new)

    async def leave(self, mid, uid):
        return await self._op('leave', mid, uid)

    async def clear(self, mid):
        return await self._op('clear', mid)

    async def clear_empty(self, mid):
        return await self._op('clear_empty', mid)

    async def batch(self):
        try:
            async with asyncio.timeout(0.4):
                rows = await self.redis.eval(BATCH, 1, INDEX, RETENTION, self.offset)
            self.offset = self.offset + len(rows) if len(rows) == 50 else 0
            return [r.decode() if isinstance(r,bytes) else r for r in rows]
        except Exception:
            return []
