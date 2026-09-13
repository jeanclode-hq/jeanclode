<template>
  <UModal
    v-model:open="open"
    :title="$t('workspace.create.title')"
    :description="$t('workspace.create.description')"
    :close="dismissible ? undefined : false"
  >
    <template
      v-if="dismissible"
      #trigger
    >
      <slot />
    </template>

    <template #body>
      <form
        class="flex flex-col gap-4"
        @submit.prevent="handleCreate"
      >
        <UFormField
          :label="$t('workspace.create.nameLabel')"
          required
        >
          <UInput
            v-model="name"
            :placeholder="$t('workspace.create.namePlaceholder')"
            autofocus
            class="w-full"
          />
        </UFormField>

        <p
          v-if="errorMessage"
          class="text-sm text-red-500"
        >
          {{ errorMessage }}
        </p>

        <div class="flex justify-end gap-2">
          <UButton
            v-if="dismissible"
            :label="$t('common.cancel')"
            variant="ghost"
            color="neutral"
            @click="open = false"
          />
          <UButton
            type="submit"
            :label="$t('workspace.create.submit')"
            :loading="creating"
            :disabled="!name.trim()"
          />
        </div>
      </form>
    </template>
  </UModal>
</template>

<script setup lang="ts">
withDefaults(defineProps<{
  dismissible?: boolean
}>(), {
  dismissible: true,
})

const emit = defineEmits<{
  created: []
}>()

const open = defineModel<boolean>('open', { default: false })

const workspaceStore = useWorkspaceStore()
const name = ref('')
const creating = ref(false)
const errorMessage = ref('')

async function handleCreate() {
  if (!name.value.trim()) return

  creating.value = true
  errorMessage.value = ''

  try {
    await workspaceStore.createWorkspace(name.value.trim())
    open.value = false
    name.value = ''
    emit('created')
  } catch (e: unknown) {
    const detail = (e as { data?: { detail?: string } })?.data?.detail
    errorMessage.value = detail || 'Failed to create workspace'
  } finally {
    creating.value = false
  }
}

// Reset form when modal opens
watch(open, (isOpen) => {
  if (isOpen) {
    name.value = ''
    errorMessage.value = ''
  }
})
</script>
