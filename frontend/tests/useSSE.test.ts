import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

describe('SSE reconnection constants', () => {
  it('exports correct reconnection constants', async () => {
    const mod = await import('../app/composables/useSSE')
    expect(mod.BASE_RECONNECT_DELAY).toBe(1000)
    expect(mod.MAX_RECONNECT_DELAY).toBe(30000)
    expect(mod.HEARTBEAT_TIMEOUT).toBe(45000)
    expect(mod.HEARTBEAT_CHECK_INTERVAL).toBe(10000)
  })
})

describe('exponential backoff calculation', () => {
  it('calculates correct delays with exponential backoff capped at MAX', async () => {
    const { BASE_RECONNECT_DELAY, MAX_RECONNECT_DELAY } = await import('../app/composables/useSSE')

    const calcDelay = (attempt: number) =>
      Math.min(BASE_RECONNECT_DELAY * 2 ** attempt, MAX_RECONNECT_DELAY)

    expect(calcDelay(0)).toBe(1000) // 1s
    expect(calcDelay(1)).toBe(2000) // 2s
    expect(calcDelay(2)).toBe(4000) // 4s
    expect(calcDelay(3)).toBe(8000) // 8s
    expect(calcDelay(4)).toBe(16000) // 16s
    expect(calcDelay(5)).toBe(30000) // capped at 30s
    expect(calcDelay(10)).toBe(30000) // still capped
  })
})

describe('EVENT_CACHE_MAP', () => {
  it('maps issue events to issues cache key', async () => {
    const { EVENT_CACHE_MAP } = await import('../app/composables/useEventStream')
    expect(EVENT_CACHE_MAP['issue']).toContain('issues')
  })

  it('maps execution events to both issues and pull-requests', async () => {
    const { EVENT_CACHE_MAP } = await import('../app/composables/useEventStream')
    expect(EVENT_CACHE_MAP['execution']).toContain('issues')
    expect(EVENT_CACHE_MAP['execution']).toContain('pull-requests')
  })

  it('maps pull_request events to pull-requests cache key', async () => {
    const { EVENT_CACHE_MAP } = await import('../app/composables/useEventStream')
    expect(EVENT_CACHE_MAP['pull_request']).toContain('pull-requests')
  })

  it('returns undefined for unknown resource types', async () => {
    const { EVENT_CACHE_MAP } = await import('../app/composables/useEventStream')
    expect(EVENT_CACHE_MAP['unknown']).toBeUndefined()
  })
})

describe('heartbeat stale detection', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('detects stale connection when heartbeat timeout exceeded', async () => {
    const { HEARTBEAT_TIMEOUT, HEARTBEAT_CHECK_INTERVAL } = await import('../app/composables/useSSE')

    const lastEventTime = Date.now()
    let staleDetected = false

    const interval = setInterval(() => {
      const elapsed = Date.now() - lastEventTime
      if (elapsed > HEARTBEAT_TIMEOUT) {
        staleDetected = true
      }
    }, HEARTBEAT_CHECK_INTERVAL)

    vi.advanceTimersByTime(HEARTBEAT_TIMEOUT + HEARTBEAT_CHECK_INTERVAL)

    expect(staleDetected).toBe(true)
    clearInterval(interval)
  })

  it('does not detect stale when events are recent', async () => {
    const { HEARTBEAT_TIMEOUT, HEARTBEAT_CHECK_INTERVAL } = await import('../app/composables/useSSE')

    let lastEventTime = Date.now()
    let staleDetected = false

    const interval = setInterval(() => {
      const elapsed = Date.now() - lastEventTime
      if (elapsed > HEARTBEAT_TIMEOUT) {
        staleDetected = true
      }
    }, HEARTBEAT_CHECK_INTERVAL)

    // Simulate receiving an event before timeout
    vi.advanceTimersByTime(HEARTBEAT_CHECK_INTERVAL)
    lastEventTime = Date.now()

    vi.advanceTimersByTime(HEARTBEAT_CHECK_INTERVAL)

    expect(staleDetected).toBe(false)
    clearInterval(interval)
  })
})
