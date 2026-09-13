/**
 * Global auth middleware.
 *
 * On first load, validates the session by calling GET /auth/me.
 * Redirects unauthenticated users to /login (except for /login itself).
 * After authentication, fetches workspaces and stores the current one.
 *
 * The /admin page is exempt: it uses ADMIN_SECRET auth, not user sessions.
 * If the backend reports no git provider configured, all traffic is funneled
 * to /admin until the instance is configured.
 */
export default defineNuxtRouteMiddleware(async (to) => {
  const { data: config, refresh: refreshConfig } = useAppConfigQuery()
  if (config.value === undefined) {
    // Don't let a transient backend outage block navigation entirely —
    // fall through with config unset and let downstream checks no-op.
    await refreshConfig().catch(() => {})
  }

  // /admin authenticates with ADMIN_SECRET via the admin cookie, not the
  // user session, so it bypasses the rest of this middleware. But we still
  // block access when the instance is fully env-managed: there's nothing to
  // configure via UI, and any DB writes would be shadowed by env precedence.
  if (to.path === '/admin') {
    if (config.value?.admin_enabled === false) {
      return navigateTo('/login')
    }
    return
  }

  // If the instance has never been configured, push everything to /admin —
  // unless admin is disabled (env-managed), in which case continue normally.
  if (config.value?.setup_required && config.value?.admin_enabled) {
    return navigateTo('/admin')
  }

  // Skip auth check for login page
  if (to.path === '/login') {
    return
  }

  const { user, fetchUser } = useAuth()

  // On first load (no user cached), validate session
  if (!user.value) {
    await fetchUser()
  }

  // Still no user after fetch — redirect to login
  if (!user.value) {
    return navigateTo('/login')
  }

  // Fetch workspaces if not yet loaded
  const workspaceStore = useWorkspaceStore()
  if (workspaceStore.workspaces.length === 0) {
    await workspaceStore.fetchWorkspaces()
  }
})
