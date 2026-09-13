/**
 * Single SSE stream composable for all real-time events.
 *
 * Opens one connection to GET /stream?workspace_id=xxx. The SSE `event`
 * field is a resource type ("issue", "agent") and the payload carries the
 * action ("created", "status_changed", etc.) plus workspace_id and data.
 *
 * Call connect() once at app/layout level — all pages share
 * the same connection via ref counting.
 */

import type { SSEConnection } from './useSSE'
import { useSSE } from './useSSE'

/**
 * Maps resource event types (matching backend EventType enum)
 * to the Pinia Colada query cache keys to invalidate.
 */
const EVENT_CACHE_MAP: Record<string, string[]> = {
  issue: ['issues', 'workspace-stats'],
  pull_request: ['pull-requests', 'workspace-stats'],
  execution: ['issues', 'pull-requests', 'workspace-stats', 'executions'],
  agent: ['issues', 'pull-requests', 'workspace-stats', 'executions'],
  mapping: ['organizations', 'repos', 'source-projects'],
  sync: ['organizations', 'repos', 'source-projects', 'issues', 'workspace-stats'],
  backfill: ['issues', 'workspace-stats'],
}

// Every open tab receives the same event at the same moment. Batching a burst
// into one flush and spreading tabs over the jitter keeps them from refetching
// in the same tick.
const COALESCE_WINDOW_MS = 500
const MAX_JITTER_MS = 3000

interface InvalidationBatcherOptions {
  invalidate: (key: string, refetch: boolean) => void
  isHidden?: () => boolean
  random?: () => number
}

function createInvalidationBatcher(options: InvalidationBatcherOptions) {
  const random = options.random ?? Math.random
  const isHidden = options.isHidden
    ?? (() => typeof document !== 'undefined' && document.visibilityState === 'hidden')
  const pending = new Set<string>()
  let timer: ReturnType<typeof setTimeout> | null = null

  function flush() {
    timer = null
    // A hidden tab only marks its queries stale; Pinia Colada refetches stale
    // active queries itself when the tab becomes visible again.
    const refetch = !isHidden()
    const keys = [...pending]
    pending.clear()
    for (const key of keys) options.invalidate(key, refetch)
  }

  function schedule(keys: string[]) {
    for (const key of keys) pending.add(key)
    if (timer) return
    timer = setTimeout(flush, COALESCE_WINDOW_MS + random() * MAX_JITTER_MS)
  }

  return { schedule }
}

/**
 * Opens a single SSE connection to /stream scoped to a workspace
 * and invalidates query caches when resource events arrive.
 */
export function useEventStream(workspaceId: string): SSEConnection {
  const queryCache = useQueryCache()

  const batcher = createInvalidationBatcher({
    invalidate: (key, refetch) => queryCache.invalidateQueries({ key: [key] }, refetch),
  })

  const listeners: Record<string, (data: unknown) => void> = {}
  for (const [eventType, cacheKeys] of Object.entries(EVENT_CACHE_MAP)) {
    listeners[eventType] = () => batcher.schedule(cacheKeys)
  }

  return useSSE({
    urlPath: `/stream?workspace_id=${encodeURIComponent(workspaceId)}`,
    listeners,
    useRefCounting: true,
    logPrefix: '[SSE]',
  })
}

// Exported for testing
export { EVENT_CACHE_MAP, COALESCE_WINDOW_MS, MAX_JITTER_MS, createInvalidationBatcher }
