<script setup lang="ts">
import type { OrgResponse, RelatedRepo, RepoResponse } from '@jeanclode/api-types'

const props = defineProps<{
  repo: RepoResponse & { displayName: string, subgroupPath: string }
  /** Orgs a related repo may come from — the current org, plus (for GitLab)
   *  every other GitLab org in the workspace. */
  linkableOrgs: OrgResponse[]
}>()

const emit = defineEmits<{
  toggle: [enabled: boolean]
  related: [relatedRepoId: string, related: RelatedRepo[]]
}>()

const toast = useToast()

// Inlined by GET /repos — a query per row is what made opening a group with
// 25 repos cost 25 requests.
const related = computed(() => props.repo.related ?? [])
const excludeIds = computed(() => [props.repo.id, ...related.value.map((r) => r.id)])
const isBusy = computed(() => linkMutation.isLoading.value || unlinkMutation.isLoading.value)

// Subgroups never appear in `linkableOrgs` — only connected orgs do — so a
// related repo is placed by its root org, which is also the segment
// `shortRepoName` strips off its path.
function orgName(rootOrgId: string): string {
  return props.linkableOrgs.find((o) => o.id === rootOrgId)?.name ?? 'other group'
}

const linkMutation = useLinkRelatedRepoMutation()
const unlinkMutation = useUnlinkRelatedRepoMutation()

async function handleAdd(relatedRepoId: string) {
  if (!relatedRepoId) return
  try {
    const next = await linkMutation.mutateAsync({ repoId: props.repo.id, relatedRepoId })
    emit('related', relatedRepoId, next)
  } catch (e) {
    toast.add({ title: 'Failed to link', description: extractApiError(e, 'Could not group these repositories'), color: 'error' })
  }
}

async function handleRemove(relatedRepoId: string) {
  try {
    const next = await unlinkMutation.mutateAsync({ repoId: props.repo.id, relatedRepoId })
    emit('related', relatedRepoId, next)
  } catch (e) {
    toast.add({ title: 'Failed to remove', description: extractApiError(e, 'Could not remove repo from group'), color: 'error' })
  }
}
</script>

<template>
  <div class="flex flex-col gap-2 px-4 py-3">
    <div class="flex items-center gap-3">
      <UIcon
        name="i-lucide-book"
        class="size-4 text-neutral-400 shrink-0"
      />
      <component
        :is="repo.web_url ? 'a' : 'span'"
        :href="repo.web_url || undefined"
        :target="repo.web_url ? '_blank' : undefined"
        class="flex-1 min-w-0"
        :class="repo.web_url ? 'hover:text-neutral-900 dark:hover:text-neutral-100 transition-colors' : ''"
      >
        <TruncatedText
          :text="repo.displayName"
          class="text-sm"
          :class="repo.enabled ? 'text-neutral-700 dark:text-neutral-300' : 'text-neutral-500'"
        />
        <TruncatedText
          v-if="repo.subgroupPath"
          :text="repo.subgroupPath"
          class="text-xs text-neutral-500"
        />
      </component>
      <UIcon
        v-if="isBusy"
        name="i-lucide-loader-2"
        class="size-3.5 text-neutral-400 animate-spin shrink-0"
      />
      <RepoMapPicker
        data-guide="gitRelated"
        class="shrink-0"
        :git-orgs="linkableOrgs"
        :exclude-ids="excludeIds"
        @select="handleAdd"
      />
      <span
        v-if="!repo.enabled"
        class="text-[11px] text-neutral-500 shrink-0"
      >
        Disabled
      </span>
      <USwitch
        :model-value="repo.enabled"
        size="sm"
        @update:model-value="emit('toggle', $event)"
      />
    </div>

    <div
      v-if="related.length > 0"
      data-guide="gitRelated"
      class="flex items-center gap-1.5 overflow-x-auto scrollbar-none pl-7 min-w-0 py-0.5"
    >
      <span
        v-for="r in related"
        :key="r.id"
        class="inline-flex items-center gap-1.5 rounded-md border border-neutral-200 bg-neutral-100 dark:border-neutral-600 dark:bg-neutral-700 pl-2 pr-1 py-0.5 shrink-0"
      >
        <UIcon
          name="i-lucide-book"
          class="size-3 text-neutral-500"
        />
        <span class="inline-flex items-center text-xs font-medium text-neutral-700 dark:text-neutral-300">
          <span
            v-if="r.root_org_id !== repo.root_org_id"
            class="text-neutral-500 shrink-0"
          >{{ orgName(r.root_org_id) }}&nbsp;/&nbsp;</span>
          <TruncatedText
            :text="shortRepoName(r.name)"
            class="max-w-40"
          />
        </span>
        <button
          type="button"
          class="cursor-pointer p-0.5 rounded hover:bg-neutral-200 dark:hover:bg-neutral-600"
          title="Remove from group"
          @click="handleRemove(r.id)"
        >
          <UIcon
            name="i-lucide-x"
            class="size-3 text-neutral-400 hover:text-neutral-600 dark:hover:text-neutral-300"
          />
        </button>
      </span>
    </div>
  </div>
</template>
