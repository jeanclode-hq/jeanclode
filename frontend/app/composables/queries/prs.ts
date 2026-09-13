import type {
  PaginatedResponse,
  PRFilters,
  PullRequest,
  RepoOption,
  RepoOptionPage,
} from '~/types/api'

export function usePullRequestsQuery(
  workspaceId: MaybeRefOrGetter<string | undefined>,
  filters?: MaybeRefOrGetter<PRFilters>,
) {
  const client = useApi()

  return useQuery({
    key: () => ['pull-requests', toValue(workspaceId), toValue(filters) ?? {}],
    query: async () => {
      const wsId = toValue(workspaceId)
      if (!wsId) return { objects: [], pagination: { total: 0, limit: 25, page: 1 }, meta: { timestamp: 0 } } as PaginatedResponse<PullRequest>

      const params: Record<string, string> = {}
      const f = toValue(filters)
      if (f?.limit !== undefined) params.limit = String(f.limit)
      if (f?.page !== undefined) params.page = String(f.page)
      if (f?.search) params.search = f.search
      if (f?.status) params.status = f.status
      if (f?.org_id) params.org_id = f.org_id
      if (f?.repository_id) params.repository_id = f.repository_id
      if (f?.author) params.author = f.author
      if (f?.period) params.period = f.period
      if (f?.execution_status) params.execution_status = f.execution_status

      const { data } = await client.get<{ 200: PaginatedResponse<PullRequest> }>({
        url: '/workspaces/{workspace_id}/pull-requests',
        path: { workspace_id: wsId },
        query: params,
      })
      return data!
    },
  })
}

export const PR_REPOS_PAGE_SIZE = 25

/** Server-paginated repo filter options with a name search and append-style
 *  "load more" — backs the pull requests list repo filter dropdown. Mirrors
 *  ``useIssueRepositoriesInfinite`` / ``useOrgReposInfinite``. */
export function usePrRepositoriesInfinite(
  workspaceId: MaybeRefOrGetter<string | undefined>,
  orgId: MaybeRefOrGetter<string | undefined>,
) {
  const client = useApi()

  const search = ref('')
  const repos = ref<RepoOption[]>([])
  const total = ref(0)
  const isLoading = ref(false)
  const nextPage = ref(1)
  const hasMore = computed(() => repos.value.length < total.value)

  let seq = 0

  async function fetchPage(reset: boolean) {
    const wsId = toValue(workspaceId)
    if (!wsId) {
      repos.value = []
      total.value = 0
      return
    }
    const mySeq = ++seq
    isLoading.value = true
    const page = reset ? 1 : nextPage.value
    try {
      const { data } = await client.get<{ 200: RepoOptionPage }>({
        url: '/workspaces/{workspace_id}/pull-requests/repositories',
        path: { workspace_id: wsId },
        query: {
          org_id: toValue(orgId) || undefined,
          search: search.value.trim() || undefined,
          page: String(page),
          limit: String(PR_REPOS_PAGE_SIZE),
        },
      })
      if (mySeq !== seq) return // a newer request superseded this one
      const chunk = data?.items ?? []
      repos.value = reset ? chunk : [...repos.value, ...chunk]
      total.value = data?.total ?? 0
      nextPage.value = page + 1
    } finally {
      if (mySeq === seq) isLoading.value = false
    }
  }

  function loadMore() {
    if (hasMore.value && !isLoading.value) fetchPage(false)
  }

  function reload() {
    fetchPage(true)
  }

  watch([() => toValue(workspaceId), () => toValue(orgId)], () => fetchPage(true), {
    immediate: true,
  })

  let debounce: ReturnType<typeof setTimeout> | undefined
  watch(search, () => {
    clearTimeout(debounce)
    debounce = setTimeout(() => fetchPage(true), 250)
  })
  onScopeDispose(() => clearTimeout(debounce))

  return { search, repos, total, hasMore, isLoading, loadMore, reload }
}

// Manual triggers are review-only by design — summary runs only via
// webhook auto-trigger. The path component is parameterized to mirror
// the backend route shape, but we keep the union narrow at the type
// level so callers can't accidentally request an auto-only workflow.
export function useTriggerPrWorkflowMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async ({ prId, workflow }: { prId: string, workflow: 'review' }) => {
      const { data } = await client.post<{ 200: { execution_id: string, status: string } }>({
        url: '/pull-requests/{pr_id}/{workflow}',
        path: { pr_id: prId, workflow },
      })
      return data!
    },
    onSuccess() {
      queryCache.invalidateQueries({ key: ['pull-requests'] })
    },
  })
}
