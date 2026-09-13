<script setup lang="ts">
export interface PageNavItem {
  id: string
  label: string
  icon?: string
  disabled?: boolean
  dot?: boolean
}

defineProps<{
  items: PageNavItem[]
  modelValue: string
}>()

const emit = defineEmits<{
  'update:modelValue': [id: string]
}>()
</script>

<template>
  <nav class="flex lg:flex-col gap-1 lg:w-48 shrink-0 border-b lg:border-b-0 lg:border-r border-neutral-300 dark:border-neutral-700 pb-3 lg:pb-0 lg:pr-4 mb-6 lg:mb-0 overflow-x-auto scrollbar-none">
    <button
      v-for="item in items"
      :key="item.id"
      type="button"
      :disabled="item.disabled"
      class="shrink-0 flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm whitespace-nowrap transition-colors"
      :class="[
        item.disabled
          ? 'opacity-25 cursor-not-allowed text-neutral-700 dark:text-neutral-400'
          : modelValue === item.id
            ? 'bg-neutral-300 dark:bg-neutral-700 text-neutral-900 dark:text-neutral-100 font-semibold cursor-pointer'
            : 'text-neutral-700 dark:text-neutral-400 hover:bg-neutral-100 dark:hover:bg-neutral-800/50 hover:text-neutral-900 dark:hover:text-neutral-100 cursor-pointer',
      ]"
      @click="!item.disabled && emit('update:modelValue', item.id)"
    >
      <slot
        :name="item.id + '-icon'"
      >
        <UIcon
          v-if="item.icon"
          :name="item.icon"
          class="size-4 shrink-0"
        />
      </slot>
      <span class="flex-1 text-left truncate">{{ item.label }}</span>
      <span
        v-if="item.dot && !item.disabled"
        class="size-2 rounded-full bg-green-500 shrink-0"
      />
    </button>
  </nav>
</template>
