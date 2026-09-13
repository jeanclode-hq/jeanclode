import type { WorkspaceExecution } from '~/types/api'

/**
 * Fetch active executions (running + queued) for the dashboard.
 */
export function useActiveExecutionsQuery(
  workspaceId: MaybeRefOrGetter<string | undefined>,
) {
  const client = useApi()

  return useQuery({
    key: () => ['executions', 'active', toValue(workspaceId)],
    query: async () => {
      const wsId = toValue(workspaceId)
      if (!wsId) return null

      const { data } = await client.get<{ 200: { items: WorkspaceExecution[] } }>({
        url: '/workspaces/{workspace_id}/executions/active',
        path: { workspace_id: wsId },
      })

      return data?.items ?? []
    },
  })
}
