/**
 * Extract a human-readable error message from an API error.
 *
 * hey-api client errors can be nested in various shapes depending
 * on how the error propagates through Pinia Colada's mutateAsync.
 */
/** FastAPI's shape for a Pydantic `ValidationError` passed straight to `HTTPException(detail=...)`. */
function describeDetail(detail: unknown): string | null {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const msgs = detail
      .map((d) => (d && typeof d === 'object' && 'msg' in d ? String((d as Record<string, unknown>).msg) : null))
      .filter((m): m is string => !!m)
    if (msgs.length) return msgs.join('; ')
  }
  return null
}

export function extractApiError(e: unknown, fallback: string): string {
  if (!e || typeof e !== 'object') return fallback

  const err = e as Record<string, unknown>

  // Direct: { detail: "..." } or { detail: [{msg: "..."}, ...] }
  const direct = describeDetail(err.detail)
  if (direct) return direct

  // Nested: { data: { detail: "..." } }
  if (err.data && typeof err.data === 'object') {
    const data = err.data as Record<string, unknown>
    const nested = describeDetail(data.detail)
    if (nested) return nested
  }

  // hey-api: { error: { detail: "..." } }
  if (err.error && typeof err.error === 'object') {
    const error = err.error as Record<string, unknown>
    const nested = describeDetail(error.detail)
    if (nested) return nested
  }

  // Wrapped: { body: { detail: "..." } }
  if (err.body && typeof err.body === 'object') {
    const body = err.body as Record<string, unknown>
    const nested = describeDetail(body.detail)
    if (nested) return nested
  }

  // Error message — sometimes this is itself the raw JSON response body
  // (e.g. `{"detail":"..."}"`) rather than a human sentence, when none of
  // the structured shapes above matched. Try to unwrap it before showing
  // it to the user.
  if (typeof err.message === 'string') {
    const trimmed = err.message.trim()
    if (trimmed.startsWith('{')) {
      try {
        const parsed = JSON.parse(trimmed) as Record<string, unknown>
        const unwrapped = describeDetail(parsed.detail)
        if (unwrapped) return unwrapped
      } catch {
        // not JSON after all — fall through to the raw message
      }
    }
    return err.message
  }

  return fallback
}
