<script setup lang="ts">
import type { OrgResponse, RelatedRepo, RelatedRepoSettings } from '@jeanclode/api-types'

const props = defineProps<{
  org: OrgResponse
  workspaceId: string
}>()

const emit = defineEmits<{
  back: []
  deleted: []
}>()

const toast = useToast()
const deleteMutation = useDeleteOrgMutation()
const isDeleting = deleteMutation.isLoading

// Server-paginated repo list (25 at a time, name filter, "load more" on scroll)
// so a group with hundreds of repos never mounts hundreds of rows — each row
// fires its own related-repos query.
const {
  search: repoSearch,
  repos: rawRepos,
  total: repoTotal,
  enabledCount: enabledRepoCount,
  hasMore: hasMoreRepos,
  isLoading: isFetchingRepos,
  loadMore: loadMoreRepos,
  reload: reloadRepos,
  setRepoEnabled,
  setRepoRelated,
  setAllReposEnabled,
} = useOrgReposInfinite(() => props.org.id)

const isInitialRepoLoad = computed(() => isFetchingRepos.value && rawRepos.value.length === 0)

// Repo enable/disable
const repoSettingsMutation = useUpdateRepoSettingsMutation()

async function toggleRepo(repoId: string, enabled: boolean) {
  try {
    await repoSettingsMutation.mutateAsync({ repoId, orgId: props.org.id, enabled })
    setRepoEnabled(repoId, enabled)
  } catch {
    toast.add({ title: 'Failed to save', description: 'Could not update repo settings', color: 'error' })
  }
}

// Bulk toggle. It flips whatever the list is showing — with a filter typed,
// that's the filtered set, not the whole org.
const bulkRepoSettingsMutation = useBulkUpdateRepoSettingsMutation()
const isBulkTogglingRepos = bulkRepoSettingsMutation.isLoading

// Every repo off is the only state where the button flips to "Enable all";
// a single one left on still reads as "there is something to turn off".
const bulkEnableTarget = computed(() => repoTotal.value > 0 && enabledRepoCount.value === 0)

const bulkToggleHint = computed(() => {
  const scope = repoSearch.value.trim() ? 'every repository matching the filter' : 'every repository in this group'
  return bulkEnableTarget.value
    ? `Turn triggers back on for ${scope}`
    : `Stop all triggers on ${scope} — manual runs, labels and mentions all go inert`
})

async function toggleAllRepos() {
  const enabled = bulkEnableTarget.value
  try {
    const result = await bulkRepoSettingsMutation.mutateAsync({
      orgId: props.org.id,
      enabled,
      search: repoSearch.value.trim() || undefined,
    })
    setAllReposEnabled(enabled)
    const count = result.updated
    toast.add({
      title: enabled ? 'Repositories enabled' : 'Repositories disabled',
      description: `${count} ${count === 1 ? 'repository' : 'repositories'} updated`,
      color: 'success',
    })
  } catch {
    toast.add({ title: 'Failed to save', description: 'Could not update repo settings', color: 'error' })
    reloadRepos()
  }
}

// Settings
const { data: settings } = useOrgSettingsQuery(() => props.org.id)
const settingsMutation = useUpdateOrgSettingsMutation()

// This component only ever renders git orgs, so the settings are GitOrgSettings.
const manageProjectWebhooks = computed(
  () => (settings.value as { manage_project_webhooks?: boolean } | undefined)?.manage_project_webhooks ?? false,
)

// -- Who can trigger @jeanclode-bot --
// Mirrors GitHub/GitLab's own "needs write/Developer+ access to trigger CI
// from a comment" convention. "Anyone" drops that check entirely, so a
// commenter with only read access can still invoke the bot.
const triggerPermissionOptions = [
  { label: 'Developers only', value: 'developer_only' },
  { label: 'Anyone', value: 'anyone' },
]

const triggerPermission = computed(
  () => (settings.value as { trigger_permission?: string } | undefined)?.trigger_permission ?? 'developer_only',
)

async function updateTriggerPermission(value: string) {
  try {
    await settingsMutation.mutateAsync({
      orgId: props.org.id,
      settings: { trigger_permission: value },
    })
  } catch {
    toast.add({ title: 'Failed to save', description: 'Could not update settings', color: 'error' })
  }
}

// -- Notify list --
// Members come from the org's provider memberships, not the workspace, so
// a maintainer who has never logged into Jeanclode is still selectable.
const { data: orgMembers, isLoading: isLoadingMembers } = useOrgMembersQuery(() => props.org.id)

const memberOptions = computed(() =>
  (orgMembers.value ?? []).map((m) => ({
    label: m.username,
    value: m.provider_identity_id,
    avatar: m.avatar_url ? { src: m.avatar_url, alt: m.username } : undefined,
  })),
)

// Cast for the same reason as manageProjectWebhooks above: the query's type is
// the settings union, and this component only ever renders git orgs.
const notifyOnReady = computed<string[]>(
  () => (settings.value as { notify?: { on_ready?: string[] } } | undefined)?.notify?.on_ready ?? [],
)

// An id whose member is gone from the org would silently vanish from the
// picker while still sitting in settings, so surface it rather than hide it.
const staleNotifyCount = computed(() => {
  const known = new Set(memberOptions.value.map((o) => o.value))
  return notifyOnReady.value.filter((id: string) => !known.has(id)).length
})

async function updateNotifyOnReady(ids: string[]) {
  try {
    await settingsMutation.mutateAsync({
      orgId: props.org.id,
      settings: { notify: { on_ready: ids } },
    })
  } catch {
    toast.add({ title: 'Failed to save', description: 'Could not update settings', color: 'error' })
  }
}

// -- Workspace repos (what else gets cloned into a run) --
// Only issue-resolve and sentry-fix clone more than one repo; review,
// summary and respond stay single-repo, so this section says "runs that
// span repos" rather than promising every workflow.
const relatedRepoSettings = computed<RelatedRepoSettings>(
  () => (settings.value as { related_repos?: RelatedRepoSettings } | undefined)?.related_repos ?? {},
)

const alwaysInclude = computed<string[]>(() => relatedRepoSettings.value.always_include ?? [])
const packSubgroup = computed(() => relatedRepoSettings.value.pack_subgroup ?? true)
const excludedSubgroups = computed<string[]>(() => relatedRepoSettings.value.excluded_subgroups ?? [])

const { data: pinnedRepos } = useRepoLookupQuery(alwaysInclude)
const { data: subgroupData, isLoading: isLoadingSubgroups } = useOrgSubgroupsQuery(() => props.org.id)

const maxPackSize = computed(() => subgroupData.value?.max_pack_size ?? 0)

// The count is what makes the choice obvious — a 250-project subgroup is
// over the cap and never packs, and the label says so rather than leaving
// the tenant to find out from a slow run.
const subgroupOptions = computed(() =>
  (subgroupData.value?.items ?? []).map((sub) => ({
    label: `${sub.name} (${sub.repo_count})`,
    value: sub.id,
    disabled: sub.repo_count > maxPackSize.value,
  })),
)

const oversizedSubgroupCount = computed(
  () => (subgroupData.value?.items ?? []).filter((s) => s.repo_count > maxPackSize.value).length,
)

async function updateRelatedRepos(patch: Partial<RelatedRepoSettings>) {
  try {
    await settingsMutation.mutateAsync({
      orgId: props.org.id,
      settings: { related_repos: patch },
    })
  } catch {
    toast.add({ title: 'Failed to save', description: 'Could not update settings', color: 'error' })
  }
}

function addAlwaysInclude(repoId: string) {
  if (alwaysInclude.value.includes(repoId)) return
  updateRelatedRepos({ always_include: [...alwaysInclude.value, repoId] })
}

function removeAlwaysInclude(repoId: string) {
  updateRelatedRepos({ always_include: alwaysInclude.value.filter((id) => id !== repoId) })
}

async function updateManageProjectWebhooks(value: boolean) {
  try {
    await settingsMutation.mutateAsync({
      orgId: props.org.id,
      settings: { manage_project_webhooks: value },
    })
  } catch {
    toast.add({ title: 'Failed to save', description: 'Could not update settings', color: 'error' })
  }
}

const providerLabel = computed(() => props.org.provider === 'github' ? 'GitHub' : 'GitLab')

// Manual repo re-sync. Webhooks are the normal path for a new repo, but
// deliveries get dropped and a GitLab instance may have no system hook at
// all — this re-runs the provider scan that the connection ran.
const syncReposMutation = useSyncOrgReposMutation()
const isSyncingRepos = syncReposMutation.isLoading

// The sync runs in the background, so the list is re-read a few times while
// it lands rather than once, immediately, into an unchanged response.
const RELOAD_DELAYS_MS = [2000, 6000, 15000]
const reloadTimers: ReturnType<typeof setTimeout>[] = []

function scheduleRepoReloads() {
  for (const delay of RELOAD_DELAYS_MS) {
    reloadTimers.push(setTimeout(reloadRepos, delay))
  }
}

onScopeDispose(() => {
  for (const timer of reloadTimers) clearTimeout(timer)
})

async function handleSyncRepos() {
  try {
    await syncReposMutation.mutateAsync(props.org.id)
    reloadRepos()
    scheduleRepoReloads()
    toast.add({
      title: 'Sync started',
      description: 'Scanning for repositories — new ones appear here as they land.',
      color: 'success',
    })
  } catch {
    toast.add({
      title: 'Sync failed',
      description: `Could not start the repository sync for this ${providerLabel.value} organization`,
      color: 'error',
    })
  }
}

async function handleDelete() {
  try {
    await deleteMutation.mutateAsync(props.org.id)
    emit('deleted')
  } catch {
    toast.add({ title: 'Delete failed', description: 'Failed to disconnect the organization', color: 'error' })
  }
}

// A link/unlink changes both sides of the pair, but the response only
// describes the repo it was called on — mirror the edge onto the other row
// when it happens to be on screen.
function applyRelated(repoId: string, relatedRepoId: string, related: RelatedRepo[]) {
  setRepoRelated(repoId, related)

  const self = rawRepos.value.find((r) => r.id === repoId)
  const other = rawRepos.value.find((r) => r.id === relatedRepoId)
  if (!self || !other) return

  const mirrored = (other.related ?? []).filter((r) => r.id !== repoId)
  if (related.some((r) => r.id === relatedRepoId)) {
    mirrored.push({ id: self.id, root_org_id: self.root_org_id, name: self.name })
  }
  setRepoRelated(relatedRepoId, mirrored)
}

// Repos can be grouped with any GitLab repo across the workspace (GitLab group
// tokens are all dispatched together); GitHub stays within its own org.
const { data: workspaceGitlabOrgs, status: workspaceGitlabOrgsStatus } = useOrgsQuery(
  () => props.workspaceId,
  () => (props.org.provider === 'gitlab' ? 'gitlab' : undefined),
)
const linkableOrgs = computed<OrgResponse[]>(() => {
  if (props.org.provider !== 'gitlab') return [props.org]
  // Hold off until the workspace org list resolves. A premature single-entry
  // list ([props.org] only) makes the picker treat it as "one org" and
  // auto-skip the org-picking step — so it lands pre-selected on the current
  // org instead of on step 1.
  if (workspaceGitlabOrgsStatus.value === 'pending') return []
  const orgs = workspaceGitlabOrgs.value ?? []
  return orgs.some((o) => o.id === props.org.id) ? orgs : [props.org, ...orgs]
})

// Decorate the current page with the GitLab display-name / subgroup split.
const displayedRepos = computed(() => {
  const isGitlab = props.org.provider === 'gitlab'
  return rawRepos.value.map((repo) => {
    const parts = repo.name.split('/')
    return {
      ...repo,
      displayName: isGitlab ? (parts[parts.length - 1] ?? repo.name) : repo.name,
      subgroupPath: isGitlab ? parts.slice(1, -1).join('/') : '',
    }
  })
})

const repoScrollRoot = ref<HTMLElement | null>(null)
const repoSentinel = ref<HTMLElement | null>(null)
let repoObserver: IntersectionObserver | null = null

function wireRepoObserver() {
  repoObserver?.disconnect()
  if (!repoSentinel.value) return
  repoObserver = new IntersectionObserver(
    (entries) => {
      if (entries[0]?.isIntersecting) loadMoreRepos()
    },
    { root: repoScrollRoot.value, rootMargin: '300px' },
  )
  repoObserver.observe(repoSentinel.value)
}

onMounted(wireRepoObserver)
onBeforeUnmount(() => repoObserver?.disconnect())
watch([repoSentinel, repoScrollRoot], wireRepoObserver)
</script>

<template>
  <div class="space-y-6">
    <!-- Back breadcrumb -->
    <button
      type="button"
      class="cursor-pointer flex items-center gap-1.5 text-xs text-neutral-500 dark:text-neutral-400 hover:text-neutral-700 dark:hover:text-neutral-300 transition-colors"
      @click="emit('back')"
    >
      <UIcon
        name="i-lucide-arrow-left"
        class="size-3.5"
      />
      Back to {{ providerLabel }}
    </button>

    <!-- Org header -->
    <div class="rounded-xl border border-neutral-200 dark:border-neutral-700 p-5">
      <div class="flex items-start gap-4">
        <UAvatar
          :src="org.avatar_url ?? undefined"
          :text="org.name.charAt(0).toUpperCase()"
          size="xl"
        />
        <div class="flex-1 min-w-0">
          <h3 class="text-lg font-semibold text-neutral-900 dark:text-neutral-100">
            {{ org.name }}
          </h3>
          <p class="text-sm text-neutral-500 dark:text-neutral-400 mt-0.5">
            {{ providerLabel }} &middot; {{ org.repo_count }} {{ org.repo_count === 1 ? 'repository' : 'repositories' }}
            <template v-if="org.base_url">
              &middot; {{ org.base_url }}
            </template>
          </p>
        </div>
        <UTooltip :text="`Re-scan ${providerLabel} for repositories this organization owns`">
          <UButton
            icon="i-lucide-refresh-cw"
            color="neutral"
            variant="outline"
            size="sm"
            :loading="isSyncingRepos"
            @click="handleSyncRepos"
          >
            Sync repos
          </UButton>
        </UTooltip>
      </div>
    </div>

    <!-- Repositories -->
    <section>
      <div class="flex items-center justify-between mb-3">
        <h4 class="text-sm font-semibold text-neutral-900 dark:text-neutral-100">
          Repositories
        </h4>
        <div class="flex items-center gap-3">
          <UTooltip
            v-if="repoTotal > 0"
            :text="bulkToggleHint"
          >
            <UButton
              :icon="bulkEnableTarget ? 'i-lucide-toggle-right' : 'i-lucide-toggle-left'"
              color="neutral"
              variant="ghost"
              size="xs"
              :loading="isBulkTogglingRepos"
              @click="toggleAllRepos"
            >
              {{ bulkEnableTarget ? 'Enable all' : 'Disable all' }}
            </UButton>
          </UTooltip>
          <span
            v-if="repoTotal > 0"
            class="text-xs text-neutral-400"
          >{{ repoTotal }} total</span>
        </div>
      </div>

      <div
        v-if="isInitialRepoLoad"
        class="rounded-xl border border-neutral-200 dark:border-neutral-700 p-8 flex items-center justify-center"
      >
        <UIcon
          name="i-lucide-loader-2"
          class="size-5 text-neutral-400 animate-spin"
        />
      </div>

      <div
        v-else
        class="rounded-xl border border-neutral-200 dark:border-neutral-700 overflow-hidden"
      >
        <div class="border-b border-neutral-200 dark:border-neutral-700 p-2">
          <UInput
            v-model="repoSearch"
            placeholder="Filter repositories..."
            icon="i-lucide-search"
            size="sm"
            variant="none"
            class="w-full"
          />
        </div>

        <div
          ref="repoScrollRoot"
          class="divide-y divide-neutral-100 dark:divide-neutral-800 max-h-[28rem] overflow-y-auto"
        >
          <GitOrgRepoRow
            v-for="repo in displayedRepos"
            :key="repo.id"
            :repo="repo"
            :linkable-orgs="linkableOrgs"
            @toggle="toggleRepo(repo.id, $event)"
            @related="(relatedRepoId, related) => applyRelated(repo.id, relatedRepoId, related)"
          />

          <div
            v-if="displayedRepos.length === 0 && !isFetchingRepos"
            class="px-4 py-8 text-center text-xs text-neutral-400"
          >
            {{ repoSearch.trim() ? 'No repositories match your filter.' : 'No repositories synced yet.' }}
          </div>

          <div
            v-if="hasMoreRepos"
            ref="repoSentinel"
            class="flex items-center justify-center py-3"
          >
            <UIcon
              name="i-lucide-loader-2"
              class="size-4 text-neutral-400 animate-spin"
            />
          </div>
        </div>

        <div
          v-if="repoTotal > 0"
          class="border-t border-neutral-200 dark:border-neutral-700 px-4 py-1.5 text-[11px] text-neutral-400 tabular-nums"
        >
          Showing {{ displayedRepos.length }} of {{ repoTotal }}
        </div>
      </div>
    </section>

    <!-- Who can trigger @jeanclode-bot -->
    <section v-if="settings">
      <h4 class="text-sm font-semibold text-neutral-900 dark:text-neutral-100 mb-3">
        Who can trigger
      </h4>
      <div class="rounded-xl border border-neutral-200 dark:border-neutral-700 overflow-hidden">
        <div class="px-4 py-3.5 flex items-start justify-between gap-3">
          <div class="min-w-0">
            <p class="text-sm text-neutral-900 dark:text-neutral-100">
              @jeanclode-bot mentions
            </p>
            <p class="text-[11px] text-neutral-500 dark:text-neutral-400 mt-0.5">
              Developers only requires the commenter to have write access to the repository —
              the same rule {{ providerLabel }} itself uses to gate triggering CI from a comment.
              Anyone skips that check, so anyone who can comment (including outside contributors
              on a public repo) can invoke the bot.
            </p>
          </div>
          <USelect
            :model-value="triggerPermission"
            :items="triggerPermissionOptions"
            value-key="value"
            size="sm"
            class="w-40 shrink-0"
            @update:model-value="updateTriggerPermission($event as string)"
          />
        </div>
      </div>
    </section>

    <!-- Notifications -->
    <section v-if="settings">
      <h4 class="text-sm font-semibold text-neutral-900 dark:text-neutral-100 mb-3">
        Notifications
      </h4>
      <div class="rounded-xl border border-neutral-200 dark:border-neutral-700 overflow-hidden">
        <div class="px-4 py-3.5 space-y-2">
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0">
              <p class="text-sm text-neutral-900 dark:text-neutral-100">
                Notify when ready
              </p>
              <p class="text-[11px] text-neutral-500 dark:text-neutral-400 mt-0.5">
                These people get @-mentioned in a comment once Jeanclode is done with a PR
                it opened — the review came back clean, or every finding has been dealt with.
                Nothing is posted on PRs you opened yourself.
              </p>
            </div>
            <USelectMenu
              :model-value="notifyOnReady"
              :items="memberOptions"
              :loading="isLoadingMembers"
              value-key="value"
              multiple
              searchable
              size="sm"
              icon="i-lucide-at-sign"
              placeholder="Nobody"
              class="w-48 sm:w-52 shrink-0"
              @update:model-value="updateNotifyOnReady($event as string[])"
            />
          </div>
          <p
            v-if="staleNotifyCount"
            class="text-[11px] text-amber-600 dark:text-amber-500"
          >
            {{ staleNotifyCount }} selected
            {{ staleNotifyCount === 1 ? 'member is' : 'members are' }}
            no longer in this {{ org.provider === 'gitlab' ? 'group' : 'organization' }} and
            won't be notified.
          </p>
          <p
            v-else-if="!isLoadingMembers && !memberOptions.length"
            class="text-[11px] text-neutral-500 dark:text-neutral-400"
          >
            No members synced yet. Re-sync the {{ org.provider === 'gitlab' ? 'group' : 'organization' }}
            if this looks wrong.
          </p>
        </div>
      </div>
    </section>

    <!-- Workspace repos (what a multi-repo run clones alongside its own repo) -->
    <section v-if="settings">
      <h4 class="text-sm font-semibold text-neutral-900 dark:text-neutral-100 mb-3">
        Workspace repositories
      </h4>
      <div class="rounded-xl border border-neutral-200 dark:border-neutral-700 overflow-hidden divide-y divide-neutral-200 dark:divide-neutral-700">
        <div class="px-4 py-3.5 space-y-2">
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0">
              <p class="text-sm text-neutral-900 dark:text-neutral-100">
                Always include
              </p>
              <p class="text-[11px] text-neutral-500 dark:text-neutral-400 mt-0.5">
                Cloned into every run that can span repos — issue resolution and Sentry fixes.
                Use it when the issues live in one repo and the code lives in another.
                A run on one of these repos doesn't pull the others in.
              </p>
            </div>
            <RepoMapPicker
              class="shrink-0 mt-0.5"
              :git-orgs="[org]"
              :exclude-ids="alwaysInclude"
              @select="addAlwaysInclude"
            />
          </div>
          <div
            v-if="pinnedRepos?.length"
            class="flex items-center gap-1.5 flex-wrap"
          >
            <span
              v-for="repo in pinnedRepos"
              :key="repo.id"
              class="inline-flex items-center gap-1.5 rounded-md border border-neutral-200 bg-neutral-100 dark:border-neutral-700 dark:bg-neutral-800 pl-2 pr-1 py-0.5"
            >
              <UIcon
                name="i-lucide-book"
                class="size-3 text-neutral-500"
              />
              <span class="text-xs font-medium text-neutral-700 dark:text-neutral-300">
                {{ shortRepoName(repo.name) }}
              </span>
              <UButton
                icon="i-lucide-x"
                color="neutral"
                variant="ghost"
                size="xs"
                :padded="false"
                class="p-0.5"
                @click="removeAlwaysInclude(repo.id)"
              />
            </span>
          </div>
        </div>

        <div class="px-4 py-3.5 space-y-2">
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0">
              <p class="text-sm text-neutral-900 dark:text-neutral-100">
                Include the rest of the subgroup
              </p>
              <p class="text-[11px] text-neutral-500 dark:text-neutral-400 mt-0.5">
                A run also clones the other repositories sitting directly in its own subgroup —
                not the whole {{ org.provider === 'gitlab' ? 'group' : 'organization' }}, and not
                nested subgroups. Subgroups holding more than {{ maxPackSize }} repositories are
                never packed: cloning them would take longer than the run itself.
              </p>
            </div>
            <USwitch
              :model-value="packSubgroup"
              size="sm"
              class="mt-0.5 shrink-0"
              @update:model-value="updateRelatedRepos({ pack_subgroup: $event })"
            />
          </div>
        </div>

        <div
          v-if="packSubgroup"
          class="px-4 py-3.5 space-y-2"
        >
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0">
              <p class="text-sm text-neutral-900 dark:text-neutral-100">
                Except these subgroups
              </p>
              <p class="text-[11px] text-neutral-500 dark:text-neutral-400 mt-0.5">
                Runs in these subgroups clone their own repository only. The number is how many
                repositories sit directly in each one.
              </p>
            </div>
            <USelectMenu
              :model-value="excludedSubgroups"
              :items="subgroupOptions"
              :loading="isLoadingSubgroups"
              value-key="value"
              multiple
              searchable
              size="sm"
              icon="i-lucide-folder-tree"
              placeholder="None"
              class="w-48 sm:w-52 shrink-0"
              @update:model-value="updateRelatedRepos({ excluded_subgroups: $event as string[] })"
            />
          </div>
          <p
            v-if="oversizedSubgroupCount"
            class="text-[11px] text-neutral-500 dark:text-neutral-400"
          >
            {{ oversizedSubgroupCount }}
            {{ oversizedSubgroupCount === 1 ? 'subgroup is' : 'subgroups are' }}
            already over the {{ maxPackSize }}-repository limit and never packed.
          </p>
        </div>
      </div>
    </section>

    <!-- Webhooks (GitLab Free — no group webhooks) -->
    <section v-if="settings && org.provider === 'gitlab'">
      <h4 class="text-sm font-semibold text-neutral-900 dark:text-neutral-100 mb-3">
        Webhooks
      </h4>
      <div class="rounded-xl border border-neutral-200 dark:border-neutral-700 overflow-hidden">
        <div class="px-4 py-3.5 flex items-start justify-between gap-3">
          <div class="min-w-0">
            <p class="text-sm text-neutral-900 dark:text-neutral-100">
              Create project webhooks
            </p>
            <p class="text-[11px] text-neutral-500 dark:text-neutral-400 mt-0.5">
              Only if you don't have a group webhook (GitLab Free). Needs a Maintainer token.
              New projects still need a system hook — set it up without merge request events.
            </p>
          </div>
          <USwitch
            :model-value="manageProjectWebhooks"
            size="sm"
            class="mt-0.5 shrink-0"
            @update:model-value="updateManageProjectWebhooks($event)"
          />
        </div>
      </div>
    </section>

    <!-- Plugins (third-party Claude Code plugins installed for this org) -->
    <PluginsSection :org-id="org.id" />

    <!-- MCP servers (org-registered remote MCP connectors, client-only) -->
    <McpServersSection :org-id="org.id" />

    <DangerZone
      title="Disconnect organization"
      description="Remove this organization and all its data. This cannot be undone."
      :loading="isDeleting"
      @confirm="handleDelete"
    />
  </div>
</template>
