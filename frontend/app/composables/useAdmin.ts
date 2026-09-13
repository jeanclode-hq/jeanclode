/**
 * Admin composable — authenticates with ADMIN_SECRET and manages
 * instance settings (git providers + LLM).
 *
 * Uses direct $fetch calls against /admin/* rather than the generated
 * SDK so the composable compiles before `make generate-types` is run.
 */
export interface GitHubConfigInput {
  client_id: string
  client_secret: string
  app_id: string
  private_key_pem: string
  webhook_secret: string
  name: string
}

export interface GitHubConfigView {
  client_id?: string | null
  client_secret?: string | null
  app_id?: string | null
  private_key_pem?: string | null
  webhook_secret?: string | null
  name?: string | null
  owner_login?: string | null
  owner_type?: string | null
}

export interface GitLabConfigInput {
  client_id: string
  client_secret: string
  instance_url: string
  webhook_secret?: string
}

export interface GitLabConfigView {
  client_id?: string | null
  client_secret?: string | null
  instance_url?: string | null
  webhook_secret?: string | null
}

/**
 * One credential in the admin-configured LLM pool (ADR-010). Ordered by
 * ``priority`` (lower tried first); dispatch automatically fails over to
 * the next non-stale row.
 */
export interface LLMCredentialInput {
  kind: 'api_key' | 'oauth_subscription'
  provider: 'claude_code' | 'anthropic' | 'openai' | 'openai_compatible'
  secret: string
  model_high: string
  model_low: string
  base_url?: string | null
  plan_tier?: 'pro' | 'max' | 'max_5x' | null
}

export interface LLMCredentialView {
  id: string
  priority: number
  kind: string
  provider: string
  plan_tier?: string | null
  secret?: string | null
  model_high: string
  model_low: string
  base_url?: string | null
  status: string
  stale_until?: string | null
}

export interface AdminSettings {
  github: GitHubConfigView | null
  gitlab: GitLabConfigView | null
}

export interface GithubManifest {
  manifest: Record<string, unknown>
  state: string
  github_url: string
}

export function useAdmin() {
  const apiBase = useApiBase()
  const headers = useRequestHeaders(['cookie'])

  const authenticated = useState<boolean>('admin.authenticated', () => false)

  async function authenticate(secret: string): Promise<boolean> {
    await $fetch(`${apiBase}/admin/auth`, {
      method: 'POST',
      credentials: 'include',
      headers,
      body: { secret },
    })
    authenticated.value = true
    return true
  }

  async function logout(): Promise<void> {
    try {
      await $fetch(`${apiBase}/admin/logout`, {
        method: 'POST',
        credentials: 'include',
        headers,
      })
    } finally {
      authenticated.value = false
    }
  }

  async function getSettings(): Promise<AdminSettings> {
    return await $fetch<AdminSettings>(`${apiBase}/admin/settings`, {
      credentials: 'include',
      headers,
    })
  }

  /**
   * Attempt to restore an admin session from an existing cookie (e.g. after
   * a full page reload from the GitHub manifest redirect). Returns settings
   * on success, null if the cookie is missing or expired.
   */
  async function restoreSession(): Promise<AdminSettings | null> {
    try {
      const s = await getSettings()
      authenticated.value = true
      return s
    } catch {
      authenticated.value = false
      return null
    }
  }

  async function updateGithub(config: GitHubConfigInput): Promise<void> {
    await $fetch(`${apiBase}/admin/settings/github`, {
      method: 'PUT',
      credentials: 'include',
      headers,
      body: config,
    })
  }

  async function updateGitlab(config: GitLabConfigInput): Promise<void> {
    await $fetch(`${apiBase}/admin/settings/gitlab`, {
      method: 'PUT',
      credentials: 'include',
      headers,
      body: config,
    })
  }

  async function deleteCategory(category: 'github' | 'gitlab'): Promise<void> {
    await $fetch(`${apiBase}/admin/settings/${category}`, {
      method: 'DELETE',
      credentials: 'include',
      headers,
    })
  }

  async function listLlmCredentials(): Promise<LLMCredentialView[]> {
    return await $fetch<LLMCredentialView[]>(`${apiBase}/admin/llm-credentials`, {
      credentials: 'include',
      headers,
    })
  }

  async function createLlmCredential(config: LLMCredentialInput): Promise<LLMCredentialView> {
    return await $fetch<LLMCredentialView>(`${apiBase}/admin/llm-credentials`, {
      method: 'POST',
      credentials: 'include',
      headers,
      body: config,
    })
  }

  async function updateLlmCredential(
    id: string,
    config: Partial<LLMCredentialInput>,
  ): Promise<LLMCredentialView> {
    return await $fetch<LLMCredentialView>(`${apiBase}/admin/llm-credentials/${id}`, {
      method: 'PUT',
      credentials: 'include',
      headers,
      body: config,
    })
  }

  async function deleteLlmCredential(id: string): Promise<void> {
    await $fetch(`${apiBase}/admin/llm-credentials/${id}`, {
      method: 'DELETE',
      credentials: 'include',
      headers,
    })
  }

  async function reorderLlmCredentials(orderedIds: string[]): Promise<LLMCredentialView[]> {
    return await $fetch<LLMCredentialView[]>(`${apiBase}/admin/llm-credentials/reorder`, {
      method: 'POST',
      credentials: 'include',
      headers,
      body: { ordered_ids: orderedIds },
    })
  }

  async function getGithubManifest(
    opts: {
      publicUrl?: string
      name?: string
      accountType?: 'personal' | 'org'
      orgSlug?: string
    } = {},
  ): Promise<GithubManifest> {
    const query: Record<string, string> = {}
    if (opts.publicUrl) query.public_url = opts.publicUrl
    if (opts.name) query.name = opts.name
    if (opts.accountType) query.account_type = opts.accountType
    if (opts.orgSlug) query.org_slug = opts.orgSlug
    return await $fetch<GithubManifest>(`${apiBase}/admin/github/manifest`, {
      credentials: 'include',
      headers,
      query,
    })
  }

  return {
    authenticated: readonly(authenticated),
    authenticate,
    restoreSession,
    logout,
    getSettings,
    updateGithub,
    updateGitlab,
    deleteCategory,
    getGithubManifest,
    listLlmCredentials,
    createLlmCredential,
    updateLlmCredential,
    deleteLlmCredential,
    reorderLlmCredentials,
  }
}
