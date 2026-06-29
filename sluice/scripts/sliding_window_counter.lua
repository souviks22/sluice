-- Sliding Window Counter: weighted interpolation over two fixed buckets
--
-- The key insight: instead of logging every request (O(N) memory),
-- we keep only the current window counter and the previous window counter,
-- then interpolate: effective_count = prev_count * overlap_ratio + cur_count
--
-- Overlap ratio = fraction of previous window still "inside" the sliding window.
-- e.g. if window=60s and we're 10s into the current minute, overlap = 50/60 ≈ 0.833
--
-- KEYS[1] = counter key prefix (e.g. "rl:swc:{identifier}")
-- ARGV[1] = limit     (max requests per window)
-- ARGV[2] = window_ms (window size in milliseconds)
-- ARGV[3] = now_ms    (current time in milliseconds)
--
-- Returns: { allowed (0|1), remaining_est, reset_after_ms, retry_after_ms }

local prefix    = KEYS[1]
local limit     = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local now_ms    = tonumber(ARGV[3])

-- Determine current and previous bucket boundaries
local bucket_id   = math.floor(now_ms / window_ms)
local cur_key     = prefix .. ":" .. tostring(bucket_id)
local prev_key    = prefix .. ":" .. tostring(bucket_id - 1)

local cur_count   = tonumber(redis.call("GET", cur_key) or "0")
local prev_count  = tonumber(redis.call("GET", prev_key) or "0")

-- Position within the current window (0..1)
local position    = (now_ms % window_ms) / window_ms
-- Fraction of previous window that overlaps with the current sliding window
local overlap     = 1.0 - position

-- Weighted estimate of requests in [now - window_ms, now]
local effective   = prev_count * overlap + cur_count

local allowed     = 0
local retry_after = 0

if effective < limit then
    redis.call("INCR", cur_key)
    redis.call("PEXPIRE", cur_key, window_ms * 2)  -- keep previous alive
    cur_count = cur_count + 1
    allowed   = 1
else
    -- Rough retry: how much time until effective drops below limit
    -- effective decreases as overlap shrinks; solve for position where it's < limit
    -- prev_count*(1-p) + cur_count < limit  =>  p > 1 - (limit - cur_count)/prev_count
    if prev_count > 0 then
        local target_overlap = (limit - cur_count) / prev_count
        if target_overlap < 0 then
            -- cur_count alone exceeds limit; wait for next window
            retry_after = math.ceil(window_ms - (now_ms % window_ms))
        else
            local target_position = 1.0 - target_overlap
            local target_ms       = math.floor(bucket_id * window_ms + target_position * window_ms)
            retry_after           = math.max(0, math.ceil(target_ms - now_ms))
        end
    else
        retry_after = math.ceil(window_ms - (now_ms % window_ms))
    end
end

local effective_new = prev_count * overlap + cur_count
local remaining     = math.max(0, math.floor(limit - effective_new))
local reset_after   = math.ceil(window_ms - (now_ms % window_ms))

return { allowed, remaining, reset_after, retry_after }
