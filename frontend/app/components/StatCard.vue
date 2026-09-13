<template>
  <div class="stat-card flex items-center gap-3 rounded-xl border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 px-4 py-3 shadow-sm hover:shadow-md hover:-translate-y-0.5 transition-all duration-200">
    <div
      class="size-8 flex items-center justify-center rounded-lg"
      :class="iconBg"
    >
      <UIcon
        :name="icon"
        class="size-4"
        :class="iconColor"
      />
    </div>
    <div>
      <p class="text-lg font-semibold tabular-nums text-neutral-900 dark:text-neutral-100">
        {{ value }}
      </p>
      <p class="text-[11px] text-neutral-500 dark:text-neutral-400">
        {{ label }}
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
defineProps<{
  icon: string
  iconBg: string
  iconColor: string
  value: number | string
  label: string
}>()
</script>
