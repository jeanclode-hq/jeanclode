<script setup lang="ts">
import type { OrgResponse } from '@jeanclode/api-types'

const props = withDefaults(defineProps<{
  gitOrgs: OrgResponse[]
  /** Repo ids to hide from the search (already linked, or the repo itself). */
  excludeIds?: string[]
}>(), {
  excludeIds: () => [],
})

const emit = defineEmits<{
  select: [repoId: string]
}>()

// Step 1: pick org, Step 2: search repo
const selectedOrgId = ref<string | null>(null)
const selectedOrg = computed(() =>
  props.gitOrgs.find((o) => o.id === selectedOrgId.value),
)

// With a single org there's nothing to pick — jump straight to the repo search.
const singleOrg = computed(() => props.gitOrgs.length === 1)
watchEffect(() => {
  if (singleOrg.value && !selectedOrgId.value && props.gitOrgs[0]) {
    selectedOrgId.value = props.gitOrgs[0].id
  }
})

// Server-paginated: the menu filter drives a backend search, results append a
// page at a time — a 900-repo org never ships more than 25 rows to the client.
const { search, repos, hasMore, isLoading, loadMore } = useOrgReposInfinite(() => selectedOrgId.value)

const repoItems = computed(() => {
  const exclude = new Set(props.excludeIds)
  return repos.value
    .filter((repo) => !exclude.has(repo.id))
    .map((repo) => ({ label: shortRepoName(repo.name), value: repo.id }))
})

function handleRepoSelect(value: string) {
  if (!value) return
  emit('select', value)
  selectedOrgId.value = null
}

function handleBack() {
  selectedOrgId.value = null
}
</script>

<template>
  <div class="flex items-center gap-1.5 min-w-0">
    <Transition
      enter-active-class="transition-all duration-200"
      enter-from-class="opacity-0 scale-95"
      enter-to-class="opacity-100 scale-100"
      leave-active-class="transition-all duration-150"
      leave-from-class="opacity-100 scale-100"
      leave-to-class="opacity-0 scale-95"
      mode="out-in"
    >
      <!-- Orgs still loading -->
      <div
        v-if="!selectedOrgId && gitOrgs.length === 0"
        key="orgs-loading"
        class="flex items-center px-2 py-1"
      >
        <UIcon
          name="i-lucide-loader-2"
          class="size-3.5 text-neutral-400 animate-spin"
        />
      </div>

      <!-- Step 1: Org pills -->
      <div
        v-else-if="!selectedOrgId"
        key="orgs"
        class="flex items-center gap-1 overflow-x-auto scrollbar-none max-w-56 sm:max-w-72"
      >
        <button
          v-for="org in gitOrgs"
          :key="org.id"
          type="button"
          class="cursor-pointer shrink-0 inline-flex items-center gap-1.5 px-2 py-1 rounded-md text-xs font-medium transition-all bg-neutral-100 dark:bg-neutral-700 text-neutral-600 dark:text-neutral-400 hover:bg-neutral-200 dark:hover:bg-neutral-600 hover:text-neutral-900 dark:hover:text-neutral-200"
          @click="selectedOrgId = org.id"
        >
          <UAvatar
            v-if="org.avatar_url"
            :src="org.avatar_url"
            size="3xs"
          />
          <UIcon
            v-else
            :name="org.provider === 'github' ? 'i-simple-icons-github' : 'i-simple-icons-gitlab'"
            class="size-3"
          />
          <TruncatedText
            :text="org.name"
            class="max-w-20"
          />
        </button>
      </div>

      <!-- Step 2: Repo search -->
      <div
        v-else
        key="search"
        class="flex items-center gap-1.5 w-56 sm:w-72"
      >
        <button
          v-if="!singleOrg"
          type="button"
          class="cursor-pointer shrink-0 p-1 rounded hover:bg-neutral-200 dark:hover:bg-neutral-600 transition-colors"
          title="Back to org selection"
          @click="handleBack"
        >
          <UIcon
            name="i-lucide-arrow-left"
            class="size-3.5 text-neutral-400"
          />
        </button>
        <UAvatar
          v-if="selectedOrg?.avatar_url"
          :src="selectedOrg.avatar_url"
          size="3xs"
          class="shrink-0"
        />
        <UIcon
          v-else-if="selectedOrg"
          :name="selectedOrg.provider === 'github' ? 'i-simple-icons-github' : 'i-simple-icons-gitlab'"
          class="size-3.5 text-neutral-400 shrink-0"
        />
        <UInputMenu
          v-model:search-term="search"
          model-value=""
          :items="repoItems"
          value-key="value"
          placeholder="Search repos..."
          icon="i-lucide-search"
          size="xs"
          class="flex-1"
          autofocus
          ignore-filter
          :highlight="false"
          :virtualize="{ overscan: 10 }"
          @update:model-value="handleRepoSelect($event as string)"
        >
          <!-- Full paths overflow the menu width — tooltip the ones that do. -->
          <template #item-label="{ item }">
            <TruncatedText :text="item.label" />
          </template>

          <template #content-bottom>
            <div
              v-if="isLoading || hasMore"
              class="border-t border-neutral-200 dark:border-neutral-800 p-1"
            >
              <button
                v-if="hasMore"
                type="button"
                class="w-full cursor-pointer rounded px-2 py-1.5 text-xs text-neutral-500 dark:text-neutral-400 hover:bg-neutral-100 dark:hover:bg-neutral-800 transition-colors"
                @click="loadMore"
              >
                {{ isLoading ? 'Loading…' : 'Load more repositories' }}
              </button>
              <p
                v-else
                class="px-2 py-1.5 text-xs text-neutral-500 text-center"
              >
                Loading…
              </p>
            </div>
          </template>
        </UInputMenu>
      </div>
    </Transition>
  </div>
</template>
