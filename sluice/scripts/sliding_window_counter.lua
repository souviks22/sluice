-- Atomic weighted sliding-window counter using one hash key (cluster-safe).
-- KEYS[1] = counter hash
-- ARGV[1] = limit, ARGV[2] = window_ms, ARGV[3] = weighted cost
-- Returns {allowed, remaining, reset_after_ms, retry_after_ms}
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local time = redis.call('TIME')
local now_ms = tonumber(time[1]) * 1000 + math.floor(tonumber(time[2]) / 1000)
local bucket_id = math.floor(now_ms / window_ms)

local state = redis.call('HMGET', key, 'bucket_id', 'current', 'previous')
local stored_bucket = tonumber(state[1])
local current = tonumber(state[2]) or 0
local previous = tonumber(state[3]) or 0
if stored_bucket == nil then
    stored_bucket = bucket_id
elseif bucket_id > stored_bucket then
    if bucket_id == stored_bucket + 1 then
        previous = current
    else
        previous = 0
    end
    current = 0
    stored_bucket = bucket_id
elseif bucket_id < stored_bucket then
    -- A backwards wall-clock adjustment must not roll state into an older bucket.
    bucket_id = stored_bucket
    now_ms = stored_bucket * window_ms
end

local position = (now_ms - bucket_id * window_ms) / window_ms
local overlap = 1 - position
local effective = previous * overlap + current
local allowed = 0
local retry_after = 0
if effective + cost <= limit then
    current = current + cost
    allowed = 1
else
    -- Conservative, bounded retry estimate. Prefer an exact point within this
    -- bucket when only the previous bucket's decaying contribution blocks it.
    local remaining_capacity = limit - cost - current
    if previous > 0 and remaining_capacity >= 0 then
        local target_overlap = remaining_capacity / previous
        local target_position = 1 - target_overlap
        retry_after = math.max(0, math.ceil((target_position - position) * window_ms))
    elseif current > 0 and remaining_capacity < 0 then
        -- Current bucket alone is too large: it must become the previous
        -- bucket and decay before this weighted request can fit.
        retry_after = math.ceil((1 - position) * window_ms + (1 - (limit - cost) / current) * window_ms)
    else
        retry_after = math.ceil((1 - position) * window_ms)
    end
end

local effective_after = previous * overlap + current
local remaining = math.max(0, math.floor(limit - effective_after))
local reset_after = math.ceil((1 - position) * window_ms)
redis.call('HSET', key, 'bucket_id', stored_bucket, 'current', current, 'previous', previous)
redis.call('PEXPIRE', key, window_ms * 2 + 1000)
return {allowed, remaining, reset_after, retry_after}
