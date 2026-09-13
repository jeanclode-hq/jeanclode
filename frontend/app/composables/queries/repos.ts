import { listRepos, listSourceProjects, triggerBackfill } from '@jeanclode/api-types'
import type { BackfillScope, RelatedRepo, RepoResponse } from '@jeanclode/api-types'
import type { ListResponse, ProjectMapping } from '~/types/api'

type ApiClient = ReturnType<typeof useApi>

export const REPOS_PAGE_SIZE = 25

/** Page through every repo in an org — for callers that genuinely need the
 *  full set (name lookups, onboarding selects). Uses a large page size so
 *  even a 900-repo group is only a handful of round trips. */
export async function fetchAllOrgRepos(client: ApiClient, orgId: string): Promise<RepoResponse[]> {
  const all: RepoResponse[] = []
  let page = 1
  for (;;) {
    const { data } = await listRepos({ client, query: { org_id: orgId, page, limit: 200 } })
    const chunk = data?.items ?? []
    all.push(...chunk)
    if (!data?.has_more || chunk.length === 0) break
    page += 1
  }
  return all
}

/** Server-paginated repo list with a name filter and append-style "load more".
 *  Backs both the org-page repo list and the related-repo picker so neither
 *  ever holds more than a page in memory. */
export function useOrgReposInfinite(orgId: MaybeRefOrGetter<string | null | undefined>) {
  const client = useApi()

  const search = ref('')
  const repos = ref<RepoResponse[]>([])
  const total = ref(0)
  const enabledCount = ref(0)
  const isLoading = ref(false)
  const nextPage = ref(1)
  const hasMore = computed(() => repos.value.length < total.value)

  let seq = 0

  async function fetchPage(reset: boolean) {
    const id = toValue(orgId)
    if (!id) {
      repos.value = []
      total.value = 0
      enabledCount.value = 0
      return
    }
    const mySeq = ++seq
    isLoading.value = true
    const page = reset ? 1 : nextPage.value
    try {
      const { data } = await listRepos({
        client,
        query: { org_id: id, page, limit: REPOS_PAGE_SIZE, search: search.value.trim() || undefined },
      })
      if (mySeq !== seq) return // a newer request superseded this one
      const chunk = data?.items ?? []
      repos.value = reset ? chunk : [...repos.value, ...chunk]
      total.value = data?.total ?? 0
      enabledCount.value = data?.enabled_count ?? 0
      nextPage.value = page + 1
    } finally {
      if (mySeq === seq) isLoading.value = false
    }
  }

  /** Reflect a settings write locally. The list is fetched imperatively, so a
   *  mutation has no cache to invalidate — without this the switch snaps back
   *  to the server value and ``enabledCount`` goes stale. */
  function setRepoEnabled(repoId: string, enabled: boolean) {
    const repo = repos.value.find((r) => r.id === repoId)
    if (!repo || repo.enabled === enabled) return
    repo.enabled = enabled
    enabledCount.value += enabled ? 1 : -1
  }

  /** Apply a repo-group change locally, same reason as ``setRepoEnabled``.
   *  ``related`` is inlined on the repo row, so the mutation response is the
   *  only fresh copy we get without refetching the page. */
  function setRepoRelated(repoId: string, related: RelatedRepo[]) {
    const repo = repos.value.find((r) => r.id === repoId)
    if (repo) repo.related = related
  }

  function setAllReposEnabled(enabled: boolean) {
    for (const repo of repos.value) repo.enabled = enabled
    enabledCount.value = enabled ? total.value : 0
  }

  function loadMore() {
    if (hasMore.value && !isLoading.value) fetchPage(false)
  }

  function reload() {
    fetchPage(true)
  }

  watch(() => toValue(orgId), () => fetchPage(true), { immediate: true })

  let debounce: ReturnType<typeof setTimeout> | undefined
  watch(search, () => {
    clearTimeout(debounce)
    debounce = setTimeout(() => fetchPage(true), 250)
  })
  onScopeDispose(() => clearTimeout(debounce))

  return {
    search,
    repos,
    total,
    enabledCount,
    hasMore,
    isLoading,
    loadMore,
    reload,
    setRepoEnabled,
    setRepoRelated,
    setAllReposEnabled,
  }
}

export function useSourceProjectsQuery(orgId: MaybeRefOrGetter<string>) {
  return useQuery({
    key: () => ['organizations', toValue(orgId), 'source-projects'],
    query: async () => {
      const { data } = await listSourceProjects({
        query: { org_id: toValue(orgId) },
      })
      return data!
    },
  })
}

export function useMappingsQuery(orgId: MaybeRefOrGetter<string>) {
  const client = useApi()

  return useQuery({
    key: () => ['organizations', toValue(orgId), 'mappings'],
    query: async () => {
      const { data } = await client.get<{ 200: ListResponse<ProjectMapping> }>({
        url: '/organizations/{org_id}/mappings',
        path: { org_id: toValue(orgId) },
      })
      return data!
    },
  })
}

export function useUpdateMappingMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (vars: { mappingId: string, repoId: string }) => {
      const { data } = await client.patch<{ 200: ProjectMapping }>({
        url: '/mappings/{mapping_id}',
        path: { mapping_id: vars.mappingId },
        body: { repo_id: vars.repoId },
      })
      return data!
    },
    onSuccess() {
      queryCache.invalidateQueries({ key: ['organizations'] })
    },
  })
}

/**
 * Import existing Sentry issues on demand.
 *
 * The run carries an explicit scope, so it proceeds even for an org whose
 * stored preference is "don't import" — skipping at onboarding is a
 * deferral, not a permanent opt-out.
 */
export function useBackfillMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (vars: { orgId: string, scope?: BackfillScope }) => {
      const { data } = await triggerBackfill({
        client,
        query: { org_id: vars.orgId },
        body: { scope: vars.scope ?? null },
      })
      return data!
    },
    onSuccess() {
      queryCache.invalidateQueries({ key: ['issues'] })
    },
  })
}
