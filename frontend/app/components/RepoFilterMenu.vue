<script setup lang="ts">
import type { RepoOption } from '~/types/api'

const modelValue = defineModel<string>({ required: true })

const props = defineProps<{
  placeholder: string
  search: string
  repos: RepoOption[]
  hasMore: boolean
  isLoading: boolean
  loadMore: () => void
}>()

const emit = defineEmits<{
  'update:search': [string]
}>()

const searchTerm = computed({
  get: () => props.search,
  set: (value: string) => emit('update:search', value),
})

const items = computed(() =>
  props.repos.map((repo) => ({ label: repo.name, value: repo.id })),
)

// Clearing lives in a sticky row above the (virtualized, auto-scrolled) list
// rather than as a list item — once a repo is picked the menu scrolls to it on
// open, which would push an "All repos" item out of view and strand the user
// with no way back to the full set.
function clearSelection() {
  modelValue.value = ''
  emit('update:search', '')
}
</script>

<template>
  <UInputMenu
    v-model="modelValue"
    v-model:search-term="searchTerm"
    :items="items"
    value-key="value"
    :placeholder="placeholder"
    icon="i-lucide-search"
    size="sm"
    class="min-w-0 w-40 shrink"
    ignore-filter
    :highlight="false"
    :virtualize="{ overscan: 10 }"
  >
    <!-- Subgroup paths still overflow the menu — tooltip the ones that do. -->
    <template #item-label="{ item }">
      <TruncatedText :text="item.label" />
    </template>

    <template
      v-if="modelValue"
      #content-top
    >
      <div class="border-b border-neutral-100 dark:border-neutral-800 p-1">
        <button
          type="button"
          class="w-full cursor-pointer rounded px-2 py-1.5 text-left text-xs text-neutral-500 dark:text-neutral-400 hover:bg-neutral-100 dark:hover:bg-neutral-800 transition-colors"
          @click="clearSelection"
        >
          {{ placeholder }}
        </button>
      </div>
    </template>

    <template #content-bottom>
      <div
        v-if="isLoading || hasMore"
        class="border-t border-neutral-100 dark:border-neutral-800 p-1"
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
          class="px-2 py-1.5 text-xs text-neutral-400 text-center"
        >
          Loading…
        </p>
      </div>
    </template>
  </UInputMenu>
</template>
