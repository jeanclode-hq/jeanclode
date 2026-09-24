import { client } from '@jeanclode/api-types'

/**
 * Returns the configured hey-api client instance.
 * Configures baseUrl and credentials on each call to pick up
 * SSR request cookies when needed.
 */
export function useApi() {
  const headers = useRequestHeaders(['cookie'])

  client.setConfig({
    baseUrl: useApiBase(),
    credentials: 'include',
    headers,
    // Without this, a non-2xx response resolves normally as { error }
    // instead of throwing — every composable in this app does
    // `const { data } = await someCall(...); return data!`, which
    // silently discards that `error` and returns `undefined` as if the
    // call had succeeded. That's why failed mutations (e.g. a 422 saving
    // a credential) never reached a try/catch or a toast — the promise
    // never rejected. Throwing here makes the existing catch blocks
    // (and extractApiError) actually fire.
    throwOnError: true,
    fetch: async (request) => guideDemoResponse(request) ?? globalThis.fetch(request),
  })

  return client
}
