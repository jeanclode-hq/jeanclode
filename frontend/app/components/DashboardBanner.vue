<script setup lang="ts">
defineProps<{
  icon: string
  iconClass?: string
  title: string
  description: string
  actionLabel: string
  variant?: 'neutral' | 'highlight'
}>()

const emit = defineEmits<{
  action: []
}>()
</script>

<template>
  <div
    class="flex items-center gap-4 p-4 rounded-xl border"
    :class="[
      variant === 'highlight'
        ? 'border-neutral-300 dark:border-neutral-600 bg-neutral-100/60 dark:bg-neutral-800/60'
        : 'border-neutral-200 dark:border-neutral-700 bg-neutral-50 dark:bg-neutral-900',
    ]"
  >
    <div
      class="size-10 rounded-lg flex items-center justify-center shrink-0"
      :class="[
        variant === 'highlight'
          ? 'bg-neutral-200 dark:bg-neutral-700'
          : 'bg-neutral-100 dark:bg-neutral-800',
      ]"
    >
      <UIcon
        :name="icon"
        class="size-5"
        :class="iconClass ?? (variant === 'highlight' ? 'text-neutral-700 dark:text-neutral-200' : 'text-neutral-400')"
      />
    </div>
    <div class="flex-1 min-w-0">
      <p class="text-sm font-semibold text-neutral-900 dark:text-neutral-100">
        {{ title }}
      </p>
      <p class="text-xs text-neutral-500 dark:text-neutral-400 mt-0.5">
        {{ description }}
      </p>
    </div>
    <UButton
      :label="actionLabel"
      size="sm"
      @click="emit('action')"
    />
  </div>
</template>
