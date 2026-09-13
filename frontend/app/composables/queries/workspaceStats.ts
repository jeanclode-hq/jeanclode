import type { DashboardStats, WorkspaceSources } from '~/types/api'

export function useWorkspaceStatsQuery(workspaceId: MaybeRefOrGetter<string | undefined>) {
  const client = useApi()

  return useQuery({
    key: () => ['workspace-stats', toValue(workspaceId)],
    query: async () => {
      const wsId = toValue(workspaceId)
      if (!wsId) return null

      const { data } = await client.get<{ 200: DashboardStats }>({
        url: '/workspaces/{workspace_id}/stats',
        path: { workspace_id: wsId },
      })
      return data!
    },
  })
}

export function useWorkspaceSourcesQuery(workspaceId: MaybeRefOrGetter<string | undefined>) {
  const client = useApi()

  return useQuery({
    key: () => ['workspace-sources', toValue(workspaceId)],
    query: async () => {
      const wsId = toValue(workspaceId)
      if (!wsId) return null

      const { data } = await client.get<{ 200: WorkspaceSources }>({
        url: '/workspaces/{workspace_id}/sources',
        path: { workspace_id: wsId },
      })
      return data!
    },
  })
}
