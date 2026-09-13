/**
 * Auth composable — manages user session state.
 *
 * - `fetchUser()` calls GET /auth/me to hydrate the current user
 * - `login(provider)` redirects to the backend OAuth flow
 * - `logout()` calls POST /auth/logout and redirects to /login
 * - `updateProfile(body)` calls PATCH /auth/me and refreshes user state
 * - `linkProvider(provider)` redirects to the backend provider-link flow
 * - `disconnectProvider(provider)` calls DELETE /auth/providers/{provider}
 * - `user` is a reactive ref to the current user profile (null if not logged in)
 *
 * Uses `useState()` for SSR-safe shared state (not module-level ref)
 * and forwards the cookie header during SSR so the backend sees the session.
 */
import type { UserProfile } from '~/types/auth'

type OAuthProvider = 'github' | 'gitlab'

interface UpdateProfileBody {
  email?: string | null
  onboarding_step?: string | null
}

interface UpdateProfileResult {
  message: string
  profile: UserProfile
}

interface DisconnectProviderResult {
  message: string
  profile: UserProfile
}

export function useAuth() {
  const user = useState<UserProfile | null>('auth.user', () => null)
  const loading = useState<boolean>('auth.loading', () => false)
  const apiBase = useApiBase()
  const headers = useRequestHeaders(['cookie'])

  async function fetchUser(): Promise<UserProfile | null> {
    loading.value = true
    try {
      const response = await $fetch<UserProfile>(`${apiBase}/auth/me`, {
        credentials: 'include',
        headers,
      })
      user.value = response
      return response
    } catch {
      user.value = null
      return null
    } finally {
      loading.value = false
    }
  }

  function login(provider: OAuthProvider) {
    window.location.href = `${apiBase}/auth/${provider}/authorize`
  }

  async function logout() {
    try {
      await $fetch(`${apiBase}/auth/logout`, {
        method: 'POST',
        credentials: 'include',
      })
    } catch {
      // Clear user even if logout request fails
    }
    user.value = null
    navigateTo('/login')
  }

  async function updateProfile(body: UpdateProfileBody): Promise<UpdateProfileResult> {
    const result = await $fetch<UpdateProfileResult>(`${apiBase}/auth/me`, {
      method: 'PATCH',
      credentials: 'include',
      body,
    })
    user.value = result.profile
    return result
  }

  function linkProvider(provider: OAuthProvider) {
    window.location.href = `${apiBase}/auth/link/${provider}`
  }

  async function disconnectProvider(provider: OAuthProvider): Promise<DisconnectProviderResult> {
    const result = await $fetch<DisconnectProviderResult>(`${apiBase}/auth/providers/${provider}`, {
      method: 'DELETE',
      credentials: 'include',
    })
    user.value = result.profile
    return result
  }

  function hasProvider(provider: OAuthProvider): boolean {
    if (!user.value) return false
    switch (provider) {
      case 'github': return !!user.value.github_external_id
      case 'gitlab': return !!user.value.gitlab_external_id
    }
  }

  const connectedProviderCount = computed(() => {
    if (!user.value) return 0
    return [
      user.value.github_external_id,
      user.value.gitlab_external_id,
    ].filter(Boolean).length
  })

  return {
    user: readonly(user),
    loading: readonly(loading),
    connectedProviderCount,
    fetchUser,
    login,
    logout,
    updateProfile,
    linkProvider,
    disconnectProvider,
    hasProvider,
  }
}
