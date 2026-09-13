/**
 * The backend base URL for the current rendering context.
 *
 * On the client it's always `public.apiBase` (what the browser can reach).
 * During SSR it's `apiInternal` when set — in Docker Compose the frontend
 * container's own `localhost` is not the backend, so a server-side fetch to
 * the public base fails with ECONNREFUSED and Nuxt renders a 500 page with
 * no XHR and no backend log. `apiInternal` points SSR at the compose service
 * name instead. Falls back to the public base when unset (kube, local dev).
 */
export function useApiBase(): string {
  const config = useRuntimeConfig()
  const publicBase = config.public.apiBase as string
  if (import.meta.server) {
    return (config.apiInternal as string) || publicBase
  }
  return publicBase
}
