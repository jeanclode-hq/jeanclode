import { getAppConfig } from '@jeanclode/api-types'

export function useAppConfigQuery() {
  const client = useApi()

  return useQuery({
    key: () => ['app-config'],
    query: async () => {
      try {
        const { data } = await getAppConfig({ client })
        return data!
      } catch (e) {
        // Rethrow as a plain Error: raw fetch/undici errors carry
        // non-POJO shapes that devalue can't serialize into the SSR
        // payload, which crashes the whole page instead of just this query.
        throw new Error(extractApiError(e, 'Failed to load app config'))
      }
    },
    staleTime: 5 * 60 * 1000, // 5 minutes — config rarely changes
  })
}
