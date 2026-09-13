<template>
  <div class="flex flex-wrap items-center justify-between gap-2 border-t border-neutral-200 dark:border-neutral-600 px-4 py-2 bg-neutral-50 dark:bg-neutral-700 shrink-0">
    <div class="flex items-center gap-3">
      <span class="hidden sm:inline text-xs text-neutral-500 dark:text-neutral-300 tabular-nums">
        Showing {{ start }}&ndash;{{ end }} of {{ total }}
      </span>
      <div class="flex items-center gap-1.5">
        <span class="text-xs text-neutral-400 dark:text-neutral-500">per page</span>
        <USelect
          :model-value="limit"
          :items="pageSizeOptions.map((n) => ({ label: String(n), value: n }))"
          value-key="value"
          size="xs"
          class="w-16"
          :ui="{ base: 'text-xs' }"
          @update:model-value="$emit('update:limit', $event)"
        />
      </div>
    </div>
    <UPagination
      v-if="total > limit"
      :page="page"
      :total="total"
      :items-per-page="limit"
      :show-edges="false"
      :sibling-count="0"
      size="xs"
      @update:page="$emit('update:page', $event)"
    />
  </div>
</template>

<script setup lang="ts">
const pageSizeOptions = [25, 50]

defineProps<{
  page: number
  limit: number
  total: number
  start: number
  end: number
}>()

defineEmits<{
  'update:page': [value: number]
  'update:limit': [value: number]
}>()
</script>
