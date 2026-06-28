-- Sliding Window Log: sorted-set of request timestamps
-- KEYS[1] = log key  (e.g. "rl:swl:{identifier}")
-- ARGV[1] = limit    (max requests per window)
-- ARGV[2] = window_ms (window size in milliseconds)
-- ARGV[3] = now_ms   (current time in milliseconds)
--
-- Returns: { allowed (0|1), remaining, reset_after_ms, retry_after_ms }
--
-- Memory note: O(N) per key where N = requests in current window.
-- Each entry is a float64 score (8 bytes) + member (8-byte unique suffix).

local key       = KEYS[1]
local limit     = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local now_ms    = tonumber(ARGV[3])

local cutoff    = now_ms - window_ms

-- Purge expired entries
redis.call("ZREMRANGEBYSCORE", key, "-inf", cutoff)

-- Count remaining in window
local count = redis.call("ZCARD", key)

local allowed     = 0
local retry_after = 0

if count < limit then
    -- member = now_ms + random suffix for uniqueness in same millisecond
    local member = tostring(now_ms) .. ":" .. tostring(redis.call("INCR", key .. ":seq"))
    redis.call("ZADD", key, now_ms, member)
    allowed = 1
else
    -- retry_after = how long until the oldest entry expires
    local oldest = redis.call("ZRANGE", key, 0, 0, "WITHSCORES")
    if oldest and oldest[2] then
        retry_after = math.max(0, math.ceil(tonumber(oldest[2]) + window_ms - now_ms))
    end
end

-- TTL slightly longer than window
redis.call("PEXPIRE", key, window_ms + 1000)
redis.call("PEXPIRE", key .. ":seq", window_ms + 1000)

local remaining   = math.max(0, limit - count - (allowed == 1 and 1 or 0))
-- reset = when window rolls from *current* request perspective
local reset_after = window_ms  -- full window

return { allowed, remaining, reset_after, retry_after }
