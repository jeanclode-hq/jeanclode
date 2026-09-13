import type {
  Issue,
  IssueDetail,
  IssueFilters,
  PaginatedResponse,
  RepoOption,
  RepoOptionPage,
} from '~/types/api'

export const ISSUE_REPOS_PAGE_SIZE = 25

/** Server-paginated repo filter options with a name search and append-style
 *  "load more" — backs the issues list repo filter dropdown so it never
 *  holds more than a page of repos in memory. Mirrors ``useOrgReposInfinite``. */
export function useIssueRepositoriesInfinite(
  workspaceId: MaybeRefOrGetter<string | undefined>,
  sourceOrgId: MaybeRefOrGetter<string | undefined>,
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
    const workspace_id = toValue(workspaceId)
    if (!workspace_id) {
      repos.value = []
      total.value = 0
      return
    }
    const mySeq = ++seq
    isLoading.value = true
    const page = reset ? 1 : nextPage.value
    try {
      const { data } = await client.get<{ 200: RepoOptionPage }>({
        url: '/issues/repositories',
        query: {
          workspace_id,
          source_org_id: toValue(sourceOrgId) || undefined,
          search: search.value.trim() || undefined,
          page: String(page),
          limit: String(ISSUE_REPOS_PAGE_SIZE),
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

  watch([() => toValue(workspaceId), () => toValue(sourceOrgId)], () => fetchPage(true), {
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

export function useIssuesQuery(filters?: MaybeRefOrGetter<IssueFilters>) {
  const client = useApi()

  return useQuery({
    key: () => ['issues', toValue(filters) ?? {}],
    query: async () => {
      const params: Record<string, string> = {}
      const f = toValue(filters)
      if (f?.workspace_id) params.workspace_id = f.workspace_id
      if (f?.limit !== undefined) params.limit = String(f.limit)
      if (f?.page !== undefined) params.page = String(f.page)
      if (f?.search) params.search = f.search
      if (f?.order_by?.length) params.order_by = f.order_by.join(',')
      if (f?.status) params.status = f.status
      if (f?.source_org_id) params.source_org_id = f.source_org_id
      if (f?.repository_id) params.repository_id = f.repository_id
      if (f?.author) params.author = f.author
      if (f?.period) params.period = f.period
      if (f?.mapped_only) params.mapped_only = String(f.mapped_only)
      if (f?.execution_status) params.execution_status = f.execution_status

      const { data } = await client.get<{ 200: PaginatedResponse<Issue> }>({
        url: '/issues',
        query: params,
      })
      return data!
    },
  })
}

export function useIssueQuery(issueId: MaybeRefOrGetter<string>, enabled?: MaybeRefOrGetter<boolean>) {
  const client = useApi()

  return useQuery({
    key: () => ['issues', toValue(issueId)],
    enabled: () => toValue(enabled) ?? true,
    query: async () => {
      const { data } = await client.get<{ 200: IssueDetail }>({
        url: '/issues/{issue_id}',
        path: { issue_id: toValue(issueId) },
      })
      return data!
    },
  })
}

export function useRetryIssueMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (issueId: string) => {
      const { data } = await client.post<{ 200: Issue }>({
        url: '/issues/{issue_id}/retry',
        path: { issue_id: issueId },
      })
      return data!
    },
    onSuccess() {
      queryCache.invalidateQueries({ key: ['issues'] })
    },
  })
}

export function useManualExecutionMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (issueId: string) => {
      const { data } = await client.post<{ 200: { execution_id: string, status: string } }>({
        url: '/issues/{issue_id}/fix',
        path: { issue_id: issueId },
      })
      return data!
    },
    onSuccess() {
      queryCache.invalidateQueries({ key: ['issues'] })
    },
  })
}

export function useResolveIssueMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (issueId: string) => {
      const { data } = await client.post<{ 200: { execution_id: string, status: string } }>({
        url: '/issues/{issue_id}/resolve',
        path: { issue_id: issueId },
      })
      return data!
    },
    onSuccess() {
      queryCache.invalidateQueries({ key: ['issues'] })
    },
  })
}

export function useCancelExecutionMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (executionId: string) => {
      const { data } = await client.post<{ 200: { execution_id: string, cancelled: boolean } }>({
        url: '/executions/{execution_id}/cancel',
        path: { execution_id: executionId },
      })
      return data!
    },
    onSuccess() {
      // Cancels an issue-linked (fix/resolve) or PR-linked (review) execution —
      // used from the issues, pull-requests, and dashboard pages.
      queryCache.invalidateQueries({ key: ['issues'] })
      queryCache.invalidateQueries({ key: ['pull-requests'] })
      queryCache.invalidateQueries({ key: ['executions'] })
    },
  })
}

export function useDismissIssueMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (issueId: string) => {
      const { data } = await client.post<{ 200: Issue }>({
        url: '/issues/{issue_id}/dismiss',
        path: { issue_id: issueId },
      })
      return data!
    },
    onSuccess() {
      queryCache.invalidateQueries({ key: ['issues'] })
    },
  })
}
