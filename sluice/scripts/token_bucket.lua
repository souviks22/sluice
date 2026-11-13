-- Atomic token bucket admission. All decisions use Redis server time.
-- KEYS[1] = bucket hash
-- ARGV[1] = capacity, ARGV[2] = refill tokens/second, ARGV[3] = weighted cost
-- Returns {allowed, remaining, reset_after_ms, retry_after_ms}
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local requested = tonumber(ARGV[3])
local time = redis.call('TIME')
local now_ms = tonumber(time[1]) * 1000 + math.floor(tonumber(time[2]) / 1000)

local data = redis.call('HMGET', key, 'tokens', 'last_refill_ms')
local tokens = tonumber(data[1])
local last_refill = tonumber(data[2])
if tokens == nil then
    tokens = capacity
    last_refill = now_ms
end
-- Never move a bucket's logical clock backwards if the host clock is adjusted.
if now_ms < last_refill then now_ms = last_refill end
local elapsed = math.max(0, now_ms - last_refill) / 1000
local available = math.min(capacity, tokens + elapsed * refill_rate)
local allowed = 0
local retry_after = 0
if available >= requested then
    available = available - requested
    allowed = 1
else
    retry_after = math.ceil(((requested - available) / refill_rate) * 1000)
end
local ttl = math.max(1, math.ceil((capacity / refill_rate) * 2))
redis.call('HSET', key, 'tokens', available, 'last_refill_ms', now_ms)
redis.call('EXPIRE', key, ttl)
local remaining = math.max(0, math.floor(available))
local reset_after = math.ceil(((capacity - available) / refill_rate) * 1000)
return {allowed, remaining, reset_after, retry_after}
