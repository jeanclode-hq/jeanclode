/**
 * Generic composable for SSE (Server-Sent Events) streams with auto-reconnection.
 *
 * Features: exponential backoff, heartbeat monitoring, ref counting,
 * onNuxtReady timing, and auto-cleanup on page unload.
 */

export interface SSEOptions {
  /** URL path for the SSE endpoint (appended to apiBase) */
  urlPath: string
  /** Map of SSE event types to handlers. Each key is an event name
   *  (matching the SSE `event:` field), each value parses the data. */
  listeners: Record<string, (data: unknown) => void>
  /** Enable connection counting for shared connections */
  useRefCounting?: boolean
  /** Log prefix for debugging */
  logPrefix?: string
}

export interface SSEConnection {
  error: Ref<string | null>
  isConnected: Ref<boolean>
  connect: () => void
  disconnect: () => void
}

// Reconnection constants
const MAX_RECONNECT_DELAY = 30000
const BASE_RECONNECT_DELAY = 1000
const HEARTBEAT_TIMEOUT = 45000 // 1.5x server's 30s keepalive
const HEARTBEAT_CHECK_INTERVAL = 10000

// Track all active SSE connections for cleanup on page unload
const activeConnections = new Set<() => void>()
let unloadHandlerRegistered = false

function registerUnloadHandler() {
  if (unloadHandlerRegistered || typeof window === 'undefined') return
  unloadHandlerRegistered = true

  window.addEventListener('beforeunload', () => {
    activeConnections.forEach((cleanup) => cleanup())
    activeConnections.clear()
  })
}

export function useSSE(options: SSEOptions): SSEConnection {
  const config = useRuntimeConfig()
  const error = ref<string | null>(null)
  const isConnected = ref(false)
  const connectionCount = ref(0)

  let eventSource: EventSource | null = null
  let reconnectTimeout: ReturnType<typeof setTimeout> | null = null
  let heartbeatInterval: ReturnType<typeof setInterval> | null = null
  let lastEventTime = 0
  let reconnectAttempts = 0
  let shouldReconnect = true

  const logPrefix = options.logPrefix || '[SSE]'

  const isClient = import.meta.client

  function cleanup() {
    if (reconnectTimeout) {
      clearTimeout(reconnectTimeout)
      reconnectTimeout = null
    }
    if (heartbeatInterval) {
      clearInterval(heartbeatInterval)
      heartbeatInterval = null
    }
    if (eventSource) {
      eventSource.close()
      eventSource = null
    }
    isConnected.value = false
  }

  function scheduleReconnect() {
    if (!shouldReconnect) return

    const delay = Math.min(BASE_RECONNECT_DELAY * 2 ** reconnectAttempts, MAX_RECONNECT_DELAY)
    reconnectAttempts++
    error.value = `Reconnecting in ${Math.round(delay / 1000)}s...`

    reconnectTimeout = setTimeout(() => {
      reconnectTimeout = null
      if (shouldReconnect) createConnection()
    }, delay)
  }

  function startHeartbeatCheck() {
    if (heartbeatInterval) clearInterval(heartbeatInterval)
    lastEventTime = Date.now()

    heartbeatInterval = setInterval(() => {
      const elapsed = Date.now() - lastEventTime
      if (elapsed > HEARTBEAT_TIMEOUT && isConnected.value) {
        cleanup()
        if (shouldReconnect) scheduleReconnect()
      }
    }, HEARTBEAT_CHECK_INTERVAL)
  }

  function createConnection() {
    if (!isClient) return

    cleanup()

    const url = `${config.public.apiBase}${options.urlPath}`

    try {
      eventSource = new EventSource(url, { withCredentials: true })

      const onActivity = () => {
        lastEventTime = Date.now()
      }

      // Register a listener for each event type
      for (const [eventType, handler] of Object.entries(options.listeners)) {
        eventSource.addEventListener(eventType, (event: MessageEvent) => {
          onActivity()
          try {
            handler(JSON.parse(event.data))
          } catch (e) {
            console.error(`${logPrefix} Failed to parse ${eventType} event:`, e)
          }
        })
      }

      // System events for heartbeat tracking
      eventSource.addEventListener('connected', onActivity)
      eventSource.addEventListener('keepalive', onActivity)

      eventSource.onopen = () => {
        isConnected.value = true
        error.value = null
        reconnectAttempts = 0
        startHeartbeatCheck()
      }

      eventSource.onerror = () => {
        cleanup()
        if (shouldReconnect) scheduleReconnect()
      }
    } catch (e) {
      console.error(`${logPrefix} Failed to create EventSource:`, e)
      error.value = e instanceof Error ? e.message : 'Failed to connect'
      if (shouldReconnect) scheduleReconnect()
    }
  }

  let pendingConnect = false

  function doConnect() {
    pendingConnect = false

    if (eventSource && isConnected.value) {
      if (options.useRefCounting) connectionCount.value++
      return
    }

    shouldReconnect = true
    reconnectAttempts = 0
    createConnection()
    connectionCount.value = options.useRefCounting ? connectionCount.value + 1 : 1
  }

  function connect() {
    if (!isClient) return

    // Prevent duplicate pending connections
    if (pendingConnect) return
    pendingConnect = true

    // Wait for Nuxt to be fully ready and add delay for HMR stability
    onNuxtReady(() => {
      if ('requestIdleCallback' in window) {
        requestIdleCallback(() => doConnect(), { timeout: 1000 })
      } else {
        setTimeout(() => doConnect(), 200)
      }
    })
  }

  function disconnect() {
    if (options.useRefCounting && --connectionCount.value > 0) return

    shouldReconnect = false
    cleanup()
    connectionCount.value = 0
  }

  // Closure for page unload cleanup — stored so we can remove it later
  let cleanupForUnload: (() => void) | null = null

  // Only register onUnmounted if we're in a component context
  // (Pinia stores don't have a component instance)
  if (getCurrentInstance()) {
    onUnmounted(() => {
      disconnect()
      if (cleanupForUnload) {
        activeConnections.delete(cleanupForUnload)
        cleanupForUnload = null
      }
    })
  }

  // Register for cleanup on page unload
  if (isClient) {
    registerUnloadHandler()
    cleanupForUnload = () => {
      shouldReconnect = false
      cleanup()
    }
    activeConnections.add(cleanupForUnload)
  }

  return { error, isConnected, connect, disconnect }
}

// Exported for testing
export { MAX_RECONNECT_DELAY, BASE_RECONNECT_DELAY, HEARTBEAT_TIMEOUT, HEARTBEAT_CHECK_INTERVAL }
