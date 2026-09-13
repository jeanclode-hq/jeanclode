import type { WorkspaceMembers } from '~/types/api'

export function useWorkspaceMembersQuery(
  workspaceId: MaybeRefOrGetter<string | undefined>,
  page: MaybeRefOrGetter<number> = 1,
) {
  const client = useApi()

  return useQuery({
    key: () => ['workspace-members', toValue(workspaceId) ?? '', String(toValue(page))],
    query: async () => {
      const wsId = toValue(workspaceId)
      if (!wsId) return null

      const { data } = await client.get<{ 200: WorkspaceMembers }>({
        url: '/workspaces/{workspace_id}/members',
        path: { workspace_id: wsId },
        query: { page: String(toValue(page)), limit: '20' },
      })
      return data!
    },
    enabled: () => !!toValue(workspaceId),
  })
}
