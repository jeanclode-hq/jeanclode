<script setup lang="ts">
defineProps<{
  title: string
  description: string
  loading?: boolean
}>()

const emit = defineEmits<{
  confirm: []
}>()

const showConfirm = ref(false)
</script>

<template>
  <section>
    <h4 class="text-sm font-semibold text-red-600 dark:text-red-400 mb-3">
      Danger Zone
    </h4>
    <div class="rounded-xl border border-red-200 dark:border-red-900/50 bg-white dark:bg-neutral-800 p-4">
      <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <div>
          <p class="text-sm text-neutral-900 dark:text-neutral-100">
            {{ title }}
          </p>
          <p class="text-xs text-neutral-500 dark:text-neutral-400 mt-0.5">
            {{ description }}
          </p>
        </div>
        <div class="flex items-center gap-2 shrink-0">
          <UButton
            v-if="!showConfirm"
            label="Disconnect"
            variant="outline"
            color="error"
            size="sm"
            @click="showConfirm = true"
          />
          <template v-else>
            <UButton
              label="Cancel"
              variant="ghost"
              color="neutral"
              size="sm"
              @click="showConfirm = false"
            />
            <UButton
              label="Confirm disconnect"
              color="error"
              size="sm"
              :loading="loading"
              @click="emit('confirm')"
            />
          </template>
        </div>
      </div>
    </div>
  </section>
</template>
