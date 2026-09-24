<template>
  <div
    class="stat-strip grid gap-px overflow-hidden rounded-xl border border-neutral-200 dark:border-neutral-700 bg-neutral-200 dark:bg-neutral-700 shadow-sm"
    :class="columns === 4 ? 'grid-cols-2 lg:grid-cols-4' : 'grid-cols-1 sm:grid-cols-3'"
  >
    <slot />
  </div>
</template>

<script setup lang="ts">
defineProps<{
  columns: 3 | 4
}>()
</script>

<style scoped>
.stat-strip :deep(.stat-enter) {
  animation: stat-rise 360ms cubic-bezier(0.22, 1, 0.36, 1) both;
}
.stat-strip :deep(> :nth-child(2) .stat-enter) { animation-delay: 50ms; }
.stat-strip :deep(> :nth-child(3) .stat-enter) { animation-delay: 100ms; }
.stat-strip :deep(> :nth-child(4) .stat-enter) { animation-delay: 150ms; }
.stat-strip :deep(.stat-enter + .stat-enter) { animation-duration: 480ms; }

@keyframes stat-rise {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: none; }
}

@media (prefers-reduced-motion: reduce) {
  .stat-strip :deep(.stat-enter) { animation: none; }
}
</style>
