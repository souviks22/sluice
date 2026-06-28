-- Token Bucket: atomic refill + consume
-- KEYS[1] = bucket key (e.g. "rl:tb:{identifier}")
-- ARGV[1] = capacity (max tokens)
-- ARGV[2] = refill_rate (tokens per second, float)
-- ARGV[3] = requested (tokens to consume, usually 1)
-- ARGV[4] = now_ms (current time in milliseconds)
--
-- Returns: { allowed (0|1), remaining, reset_after_ms, retry_after_ms }

local key          = KEYS[1]
local capacity     = tonumber(ARGV[1])
local refill_rate  = tonumber(ARGV[2])  -- tokens/sec
local requested    = tonumber(ARGV[3])
local now_ms       = tonumber(ARGV[4])

local data = redis.call("HMGET", key, "tokens", "last_refill_ms")
local tokens        = tonumber(data[1])
local last_refill   = tonumber(data[2])

-- Bootstrap on first access
if tokens == nil then
    tokens       = capacity
    last_refill  = now_ms
end

-- Refill proportional to elapsed time
local elapsed_sec = math.max(0, (now_ms - last_refill) / 1000.0)
local new_tokens  = math.min(capacity, tokens + elapsed_sec * refill_rate)

local allowed      = 0
local retry_after  = 0

if new_tokens >= requested then
    new_tokens = new_tokens - requested
    allowed    = 1
else
    -- Time until enough tokens accumulate
    local deficit  = requested - new_tokens
    retry_after    = math.ceil((deficit / refill_rate) * 1000)  -- ms
end

-- TTL: time until bucket would be fully empty at zero refill + buffer
-- We keep the key alive for 2x the full-refill window
local ttl_sec = math.ceil((capacity / refill_rate) * 2)

redis.call("HSET", key,
    "tokens",         new_tokens,
    "last_refill_ms", now_ms)
redis.call("EXPIRE", key, ttl_sec)

local remaining   = math.floor(new_tokens)
local reset_after = math.ceil(((capacity - new_tokens) / refill_rate) * 1000)  -- ms to full

return { allowed, remaining, reset_after, retry_after }
