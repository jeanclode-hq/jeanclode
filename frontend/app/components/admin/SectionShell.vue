<script setup lang="ts">
/**
 * Shared shell for each admin section — title + description header,
 * then a default slot for whichever content the section decides to show
 * (configured card, empty-state form, etc).
 */
defineProps<{
  title: string
  description: string
}>()
</script>

<template>
  <section>
    <header class="mb-5">
      <h2 class="text-lg font-medium text-neutral-900 dark:text-neutral-100">
        {{ title }}
      </h2>
      <p class="text-sm text-neutral-500 mt-1">
        {{ description }}
      </p>
    </header>
    <slot />
  </section>
</template>
