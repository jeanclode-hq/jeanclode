import type { OrgStats } from '~/types/api'

export function useOrgStatsQuery(orgId: MaybeRefOrGetter<string | undefined>) {
  const client = useApi()

  return useQuery({
    key: () => ['organizations', toValue(orgId), 'stats'],
    query: async () => {
      const id = toValue(orgId)
      if (!id) return null

      const { data } = await client.get<{ 200: OrgStats }>({
        url: '/organizations/{org_id}/stats',
        path: { org_id: id },
      })
      return data!
    },
    enabled: () => !!toValue(orgId),
  })
}
