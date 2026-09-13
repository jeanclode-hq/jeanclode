<template>
  <div
    v-if="sources.length > 0"
    class="flex items-center gap-1 p-1 rounded-lg bg-neutral-100 dark:bg-neutral-800 max-w-full overflow-x-auto scrollbar-none"
  >
    <button
      type="button"
      class="cursor-pointer shrink-0 whitespace-nowrap px-3 py-1.5 rounded-md text-xs font-medium transition-all"
      :class="!modelValue
        ? 'bg-white dark:bg-neutral-700 text-neutral-900 dark:text-neutral-100 shadow-sm'
        : 'text-neutral-500 dark:text-neutral-400 hover:text-neutral-700 dark:hover:text-neutral-200'"
      @click="$emit('update:modelValue', '')"
    >
      All sources
    </button>
    <button
      v-for="src in sources"
      :key="src.id"
      type="button"
      class="cursor-pointer shrink-0 whitespace-nowrap inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-all"
      :class="modelValue === src.id
        ? 'bg-white dark:bg-neutral-700 text-neutral-900 dark:text-neutral-100 shadow-sm'
        : 'text-neutral-500 dark:text-neutral-400 hover:text-neutral-700 dark:hover:text-neutral-200'"
      @click="$emit('update:modelValue', modelValue === src.id ? '' : src.id)"
    >
      <UAvatar
        v-if="src.avatar_url"
        :src="src.avatar_url"
        size="3xs"
      />
      <ProviderIcon
        v-else
        :light-src="getProviderIcon(src.provider).lightSrc"
        :dark-src="getProviderIcon(src.provider).darkSrc"
        :fallback="getProviderIcon(src.provider).fallback"
        class="size-3.5"
      />
      {{ src.name }}
    </button>
  </div>
</template>

<script setup lang="ts">
import type { SourceSummary } from '~/types/api'

defineProps<{
  sources: SourceSummary[]
  modelValue: string
}>()

defineEmits<{
  'update:modelValue': [value: string]
}>()
</script>
