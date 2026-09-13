import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  COALESCE_WINDOW_MS,
  MAX_JITTER_MS,
  createInvalidationBatcher,
} from '../app/composables/useEventStream'

describe('createInvalidationBatcher', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('turns a burst of events into one invalidation per key', () => {
    const invalidate = vi.fn()
    const batcher = createInvalidationBatcher({ invalidate, random: () => 0, isHidden: () => false })

    for (let i = 0; i < 10; i++) batcher.schedule(['executions', 'workspace-stats'])
    batcher.schedule(['issues'])

    vi.advanceTimersByTime(COALESCE_WINDOW_MS - 1)
    expect(invalidate).not.toHaveBeenCalled()

    vi.advanceTimersByTime(1)
    expect(invalidate.mock.calls).toEqual([
      ['executions', true],
      ['workspace-stats', true],
      ['issues', true],
    ])
  })

  it('delays the flush by the jitter, never past its bound', () => {
    const invalidate = vi.fn()
    const batcher = createInvalidationBatcher({ invalidate, random: () => 1, isHidden: () => false })

    batcher.schedule(['executions'])

    vi.advanceTimersByTime(COALESCE_WINDOW_MS + MAX_JITTER_MS - 1)
    expect(invalidate).not.toHaveBeenCalled()

    vi.advanceTimersByTime(1)
    expect(invalidate).toHaveBeenCalledTimes(1)
  })

  it('does not push the flush back when more events arrive', () => {
    const invalidate = vi.fn()
    const batcher = createInvalidationBatcher({ invalidate, random: () => 0, isHidden: () => false })

    batcher.schedule(['issues'])
    vi.advanceTimersByTime(COALESCE_WINDOW_MS - 100)
    batcher.schedule(['executions'])
    vi.advanceTimersByTime(100)

    expect(invalidate.mock.calls).toEqual([['issues', true], ['executions', true]])
  })

  it('schedules a new flush for events after the previous one', () => {
    const invalidate = vi.fn()
    const batcher = createInvalidationBatcher({ invalidate, random: () => 0, isHidden: () => false })

    batcher.schedule(['issues'])
    vi.advanceTimersByTime(COALESCE_WINDOW_MS)
    batcher.schedule(['issues'])
    vi.advanceTimersByTime(COALESCE_WINDOW_MS)

    expect(invalidate).toHaveBeenCalledTimes(2)
  })

  it('only marks queries stale in a hidden tab', () => {
    const invalidate = vi.fn()
    const batcher = createInvalidationBatcher({ invalidate, random: () => 0, isHidden: () => true })

    batcher.schedule(['executions'])
    vi.advanceTimersByTime(COALESCE_WINDOW_MS)

    expect(invalidate).toHaveBeenCalledWith('executions', false)
  })

  it('reads the tab visibility at flush time by default', () => {
    const invalidate = vi.fn()
    const batcher = createInvalidationBatcher({ invalidate, random: () => 0 })
    const visibility = vi.spyOn(document, 'visibilityState', 'get')

    visibility.mockReturnValue('hidden')
    batcher.schedule(['issues'])
    visibility.mockReturnValue('visible')
    vi.advanceTimersByTime(COALESCE_WINDOW_MS)

    expect(invalidate).toHaveBeenCalledWith('issues', true)
    visibility.mockRestore()
  })
})
