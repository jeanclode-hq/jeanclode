import type { DashboardStats, Organization } from '~/types/api'

export function useOrgQuery(orgId: MaybeRefOrGetter<string>) {
  const client = useApi()

  return useQuery({
    key: () => ['organizations', toValue(orgId)],
    query: async () => {
      const { data } = await client.get<{ 200: Organization }>({
        url: '/organizations/{org_id}',
        path: { org_id: toValue(orgId) },
      })
      return data!
    },
  })
}

export function useStatsQuery(orgId: MaybeRefOrGetter<string>) {
  const client = useApi()

  return useQuery({
    key: () => ['organizations', toValue(orgId), 'stats'],
    query: async () => {
      const { data } = await client.get<{ 200: DashboardStats }>({
        url: '/organizations/{org_id}/stats',
        path: { org_id: toValue(orgId) },
      })
      return data!
    },
  })
}
