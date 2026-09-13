<script setup lang="ts">
/**
 * Setting Row Component
 *
 * A single setting row with label, description, and control slot.
 */

defineProps<{
  label: string
  description?: string
}>()
</script>

<template>
  <div class="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
    <div class="min-w-0 sm:flex-1">
      <p class="text-sm font-medium text-neutral-900 dark:text-neutral-100">
        {{ label }}
      </p>
      <p
        v-if="description"
        class="text-xs text-neutral-500 dark:text-neutral-400"
      >
        {{ description }}
      </p>
    </div>
    <div class="shrink-0">
      <slot />
    </div>
  </div>
</template>
