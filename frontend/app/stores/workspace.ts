/**
 * Workspace store — manages current workspace selection and list.
 *
 * Persistence: localStorage for instant client-side restore,
 * plus PATCH /auth/me to persist last_workspace_id on the backend.
 */
import type { Workspace } from '~/types/api'

const STORAGE_KEY = 'jeanclode:current-workspace-id'

export const useWorkspaceStore = defineStore('workspace', () => {
  const currentWorkspace = ref<Workspace | null>(null)
  const workspaces = ref<Workspace[]>([])
  const loading = ref(true)

  const apiBase = useApiBase()
  const headers = useRequestHeaders(['cookie'])

  async function fetchWorkspaces(): Promise<Workspace[]> {
    loading.value = true
    try {
      const response = await $fetch<{ workspaces: Workspace[] }>(`${apiBase}/workspaces`, {
        credentials: 'include',
        headers,
      })
      workspaces.value = response.workspaces

      if (!currentWorkspace.value && workspaces.value.length > 0) {
        let savedId: string | null = null
        if (import.meta.client) {
          savedId = localStorage.getItem(STORAGE_KEY)
        }
        if (!savedId) {
          const { user } = useAuth()
          savedId = user.value?.last_workspace_id ?? null
        }
        const saved = savedId ? workspaces.value.find((w) => w.id === savedId) : null
        currentWorkspace.value = saved ?? workspaces.value[0]
      }

      return workspaces.value
    } catch {
      workspaces.value = []
      return []
    } finally {
      loading.value = false
    }
  }

  function switchWorkspace(workspace: Workspace) {
    currentWorkspace.value = workspace

    if (import.meta.client) {
      localStorage.setItem(STORAGE_KEY, workspace.id)
    }

    // Persist to backend (fire-and-forget)
    $fetch(`${apiBase}/auth/me`, {
      method: 'PATCH',
      credentials: 'include',
      headers,
      body: { last_workspace_id: workspace.id },
    }).catch(() => {})
  }

  async function createWorkspace(name: string): Promise<Workspace> {
    const workspace = await $fetch<Workspace>(`${apiBase}/workspaces`, {
      method: 'POST',
      credentials: 'include',
      headers,
      body: { name },
    })
    workspaces.value.push(workspace)
    switchWorkspace(workspace)
    return workspace
  }

  return {
    currentWorkspace,
    workspaces,
    loading,
    fetchWorkspaces,
    switchWorkspace,
    createWorkspace,
  }
})
