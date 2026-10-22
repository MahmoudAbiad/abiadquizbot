"""Ephemeral, account-deduplicated foreground presence; no durable player data."""
import hashlib
import os

TTL = 45
COUNT = """
local now = tonumber(redis.call('TIME')[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
return redis.call('ZCARD', KEYS[1])
"""
UPDATE = """
local now = tonumber(redis.call('TIME')[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
local previous = tonumber(redis.call('HGET', KEYS[3], ARGV[2]) or '-1')
if tonumber(ARGV[3]) > previous then
 redis.call('HSET', KEYS[3], ARGV[2], ARGV[3])
 if ARGV[4] == '1' then
  redis.call('ZADD', KEYS[2], now + tonumber(ARGV[5]), ARGV[2])
 else
  redis.call('ZREM', KEYS[2], ARGV[2])
 end
end
local latest = redis.call('ZREVRANGE', KEYS[2], 0, 0, 'WITHSCORES')
if #latest > 0 then
 redis.call('ZADD', KEYS[1], latest[2], ARGV[1])
else
 redis.call('ZREM', KEYS[1], ARGV[1])
end
redis.call('EXPIRE', KEYS[1], 120)
redis.call('EXPIRE', KEYS[2], 120)
redis.call('EXPIRE', KEYS[3], 120)
return redis.call('ZCARD', KEYS[1])
"""


async def presence(redis, uid, session=None, sequence=0, active=False):
    # Shared by all web processes, isolated even if environments share Redis.
    namespace = hashlib.sha256(os.getenv('SUPABASE_URL', '').encode()).hexdigest()[:16]
    key = f'runner:presence:{{{namespace}}}'
    if session is None:
        return int(await redis.eval(COUNT, 1, key))
    return int(await redis.eval(UPDATE, 3, key, f'{key}:{uid}:sessions',
                                f'{key}:{uid}:revisions', str(uid), str(session),
                                str(sequence), '1' if active else '0', str(TTL)))
