<script setup lang="ts">
const { t } = useI18n()
const workspaceStore = useWorkspaceStore()

useHead({
  title: () => `${workspaceStore.currentWorkspace?.name ?? t('workspace.title')} - Jeanclode`,
})
</script>

<template>
  <div class="flex flex-col gap-6">
    <div
      v-if="!workspaceStore.currentWorkspace"
      class="flex items-center justify-center py-24"
    >
      <div class="text-center">
        <UIcon
          name="i-lucide-building-2"
          class="size-16 text-neutral-300 dark:text-neutral-600 mx-auto mb-4"
        />
        <p class="text-sm text-neutral-500 dark:text-neutral-400">
          {{ $t('common.noResults') }}
        </p>
      </div>
    </div>

    <div
      v-else
      class="grid gap-4 sm:grid-cols-2"
    >
      <div class="rounded-lg border border-neutral-200 dark:border-neutral-700 p-4">
        <p class="text-xs font-medium text-neutral-500 dark:text-neutral-400 uppercase tracking-wider mb-1">
          Slug
        </p>
        <p class="text-sm font-mono text-neutral-900 dark:text-neutral-100">
          {{ workspaceStore.currentWorkspace.slug }}
        </p>
      </div>
      <div class="rounded-lg border border-neutral-200 dark:border-neutral-700 p-4">
        <p class="text-xs font-medium text-neutral-500 dark:text-neutral-400 uppercase tracking-wider mb-1">
          Created
        </p>
        <p class="text-sm text-neutral-900 dark:text-neutral-100">
          {{ new Date(workspaceStore.currentWorkspace.created_at).toLocaleDateString() }}
        </p>
      </div>
    </div>
  </div>
</template>
