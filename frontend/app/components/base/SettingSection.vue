<script setup lang="ts">
/**
 * Setting Section Component
 *
 * Groups related settings with a header and optional card container.
 */

defineProps<{
  title: string
  icon: string
  danger?: boolean
}>()
</script>

<template>
  <div class="space-y-4">
    <h3
      class="text-sm font-semibold flex items-center gap-2"
      :class="danger
        ? 'text-error-600 dark:text-error-400'
        : 'text-neutral-900 dark:text-neutral-100'"
    >
      <UIcon
        :name="icon"
        class="size-4"
      />
      {{ title }}
    </h3>

    <UCard :class="danger ? 'border-error-200 dark:border-error-800' : ''">
      <div class="space-y-4">
        <slot />
      </div>
    </UCard>
  </div>
</template>
