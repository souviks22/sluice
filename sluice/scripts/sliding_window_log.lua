-- Exact weighted sliding-window log over (now-window, now]. One sorted set,
-- one atomic admission.
-- KEYS[1] = log sorted-set key
-- ARGV[1] = limit, ARGV[2] = window_ms, ARGV[3] = weighted cost
-- Returns {allowed, remaining, reset_after_ms, retry_after_ms}
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local time = redis.call('TIME')
local now_ms = tonumber(time[1]) * 1000 + math.floor(tonumber(time[2]) / 1000)
local cutoff = now_ms - window_ms

redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
local count = redis.call('ZCARD', key)
local allowed = 0
local retry_after = 0
if count + cost <= limit then
    -- The script is serialized by Redis. The post-prune cardinality gives
    -- unique members even when many requests share the same millisecond.
    for i = 1, cost do
        local member = tostring(now_ms) .. ':' .. tostring(count + i)
        redis.call('ZADD', key, now_ms, member)
    end
    allowed = 1
else
    -- A weighted request may need multiple existing units to expire before
    -- it fits. Wait for the required-th oldest member, not just the oldest.
    local expirations_needed = count + cost - limit
    local threshold = redis.call(
        'ZRANGE', key, expirations_needed - 1, expirations_needed - 1, 'WITHSCORES'
    )
    if threshold[2] then
        retry_after = math.max(0, math.ceil(tonumber(threshold[2]) + window_ms - now_ms))
    end
end
redis.call('PEXPIRE', key, window_ms + 1000)
local remaining = math.max(0, limit - count - (allowed == 1 and cost or 0))
return {allowed, remaining, window_ms, retry_after}
