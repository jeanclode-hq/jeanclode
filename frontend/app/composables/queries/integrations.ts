import {
  bulkUpdateRepoSettings,
  deleteOrganization,
  getOrganizationSettings,
  linkRelatedRepo,
  listOrganizationMembers,
  listOrganizations,
  listOrganizationSubgroups,
  lookupRepos,
  syncOrganizationRepositories,
  unlinkRelatedRepo,
  updateOrganizationSettings,
} from '@jeanclode/api-types'
import type { GitOrgSettings, SentryOrgSettingsInput } from '@jeanclode/api-types'

/** Throw the error if hey-api returned one instead of data. */
function unwrap<T>(result: { data?: T, error?: unknown }): T {
  if (result.error || !result.data) {
    throw result.error ?? new Error('Request failed')
  }
  return result.data
}

export function useOrgsQuery(workspaceId: MaybeRefOrGetter<string>, provider?: MaybeRefOrGetter<string | string[] | undefined>) {
  const client = useApi()

  return useQuery({
    key: () => ['organizations', 'workspace', toValue(workspaceId), JSON.stringify(toValue(provider) ?? 'all')],
    query: async () => {
      const query: Record<string, string | string[]> = { workspace_id: toValue(workspaceId) }
      const p = toValue(provider)
      if (p) query.provider = p
      const { data } = await listOrganizations({ client, query })
      return data ?? []
    },
    enabled: () => !!toValue(workspaceId),
  })
}

// -- Organization members --

/**
 * The org's provider members — the candidates for the notify picker.
 *
 * Not the same set as `useWorkspaceMembersQuery`, which only lists people
 * who have logged into Jeanclode. Members synced from the provider are
 * selectable here before they ever open the dashboard.
 */
export function useOrgMembersQuery(orgId: MaybeRefOrGetter<string>) {
  const client = useApi()

  return useQuery({
    key: () => ['organizations', toValue(orgId), 'members'],
    query: async () => {
      const { data } = await listOrganizationMembers({ client, path: { org_id: toValue(orgId) } })
      return data?.members ?? []
    },
    enabled: () => !!toValue(orgId),
  })
}

// -- Organization settings --

export function useOrgSettingsQuery(orgId: MaybeRefOrGetter<string>) {
  const client = useApi()

  return useQuery({
    key: () => ['organizations', toValue(orgId), 'settings'],
    query: async () => {
      const { data } = await getOrganizationSettings({ client, path: { org_id: toValue(orgId) } })
      return data!
    },
    enabled: () => !!toValue(orgId),
  })
}

/**
 * Subgroups under a connected group, each with its direct repo count.
 *
 * Feeds the subgroup-pack exclusion picker — the counts are the point:
 * without them nobody knows which subgroups are too big to pack.
 */
export function useOrgSubgroupsQuery(orgId: MaybeRefOrGetter<string>) {
  const client = useApi()

  return useQuery({
    key: () => ['organizations', toValue(orgId), 'subgroups'],
    query: async () => {
      const { data } = await listOrganizationSubgroups({ client, path: { org_id: toValue(orgId) } })
      return data ?? { items: [], max_pack_size: 0 }
    },
    enabled: () => !!toValue(orgId),
  })
}

/**
 * Names for repo ids held in settings (the always-include list).
 *
 * Settings store ids so a rename can't stale them out, which leaves the
 * picker with nothing to show until they're resolved.
 */
export function useRepoLookupQuery(ids: MaybeRefOrGetter<string[]>) {
  const client = useApi()

  return useQuery({
    key: () => ['repos', 'lookup', [...toValue(ids)].sort().join(',')],
    query: async () => {
      const wanted = toValue(ids)
      if (!wanted.length) return []
      const { data } = await lookupRepos({ client, query: { ids: wanted } })
      return data ?? []
    },
  })
}

export function useUpdateOrgSettingsMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (vars: { orgId: string, settings: GitOrgSettings | SentryOrgSettingsInput }) => {
      const { data } = await updateOrganizationSettings({
        client,
        path: { org_id: vars.orgId },
        body: vars.settings,
      })
      return data!
    },
    onSuccess(_data, vars) {
      queryCache.invalidateQueries({ key: ['organizations', vars.orgId, 'settings'] })
    },
  })
}

// -- Repo settings --

export function useUpdateRepoSettingsMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (vars: { repoId: string, orgId: string, enabled: boolean }) => {
      const { data } = await client.patch({
        url: '/repos/{repo_id}/settings',
        path: { repo_id: vars.repoId },
        body: { enabled: vars.enabled },
      })
      return data
    },
    onSuccess(_data, vars) {
      queryCache.invalidateQueries({ key: ['organizations', vars.orgId, 'repos'] })
    },
  })
}

/** Flip every repo of an org at once. ``search`` scopes it to the repos the
 *  list is currently showing, so a filtered view never toggles hidden repos. */
export function useBulkUpdateRepoSettingsMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (vars: { orgId: string, enabled: boolean, search?: string }) => {
      return unwrap(await bulkUpdateRepoSettings({
        client,
        body: { org_id: vars.orgId, enabled: vars.enabled, search: vars.search || null },
      }))
    },
    onSuccess(_data, vars) {
      queryCache.invalidateQueries({ key: ['organizations', vars.orgId, 'repos'] })
    },
  })
}

// -- Repo groups (git repo <-> git repo, cloned together into the same workspace) --

// A repo's group members are inlined on the repo row by GET /repos, so there
// is no per-repo query here — one page of repos costs one request, not one per
// row. Both mutations return the repo's new group for the caller to apply.

export function useLinkRelatedRepoMutation() {
  const client = useApi()

  return useMutation({
    mutation: async (vars: { repoId: string, relatedRepoId: string }) => {
      return unwrap(await linkRelatedRepo({
        client,
        path: { repo_id: vars.repoId },
        body: { related_repo_id: vars.relatedRepoId },
      }))
    },
  })
}

export function useUnlinkRelatedRepoMutation() {
  const client = useApi()

  return useMutation({
    mutation: async (vars: { repoId: string, relatedRepoId: string }) => {
      return unwrap(await unlinkRelatedRepo({
        client,
        path: { repo_id: vars.repoId, related_repo_id: vars.relatedRepoId },
      }))
    },
  })
}

// -- Delete --

export function useDeleteOrgMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (orgId: string) => {
      await deleteOrganization({ client, path: { org_id: orgId } })
    },
    onSuccess() {
      queryCache.invalidateQueries({ key: ['organizations'] })
    },
  })
}

// -- Repository re-sync --

/**
 * Re-scan the provider for repositories the org should have.
 *
 * Webhooks are the normal path, but deliveries get dropped and a GitLab
 * instance may have no system hook configured — this creates whatever is
 * missing and backfills its open PRs/MRs and issues.
 */
export function useSyncOrgReposMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (orgId: string) => {
      return unwrap(await syncOrganizationRepositories({ client, path: { org_id: orgId } }))
    },
    onSuccess(_data, orgId) {
      // The sync runs in the background; SSE refreshes the counts as repos
      // land, this just clears anything stale on the way in.
      queryCache.invalidateQueries({ key: ['organizations', orgId, 'repos'] })
      queryCache.invalidateQueries({ key: ['organizations'] })
    },
  })
}
