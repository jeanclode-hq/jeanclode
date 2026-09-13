<script setup lang="ts">
/**
 * Inline delete button with a collapse-style confirm.
 *
 * Clicking the soft red button swaps in a confirm/cancel pair so the
 * admin has to explicitly accept. Parent owns the actual delete call
 * via ``@confirm`` and passes ``loading`` so the button reflects it.
 */
defineProps<{
  buttonLabel: string
  confirmMessage: string
  confirmButtonLabel: string
  cancelLabel: string
  loading?: boolean
}>()

const emit = defineEmits<{
  confirm: []
}>()

const confirming = ref(false)

function onConfirm() {
  emit('confirm')
}
</script>

<template>
  <div>
    <template v-if="confirming">
      <div class="rounded-md border border-red-200 dark:border-red-900/40 bg-red-50 dark:bg-red-950/20 p-3 text-xs text-red-900 dark:text-red-200">
        <p class="mb-2">
          {{ confirmMessage }}
        </p>
        <div class="flex gap-2">
          <UButton
            color="error"
            size="xs"
            :loading="loading"
            @click="onConfirm"
          >
            {{ confirmButtonLabel }}
          </UButton>
          <UButton
            variant="ghost"
            size="xs"
            :disabled="loading"
            @click="confirming = false"
          >
            {{ cancelLabel }}
          </UButton>
        </div>
      </div>
    </template>
    <UButton
      v-else
      variant="soft"
      color="error"
      size="sm"
      icon="i-lucide-trash-2"
      @click="confirming = true"
    >
      {{ buttonLabel }}
    </UButton>
  </div>
</template>
