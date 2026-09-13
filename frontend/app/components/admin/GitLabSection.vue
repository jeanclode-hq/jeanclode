<script setup lang="ts">
/**
 * GitLab OAuth admin section — configured-state card + setup form.
 */
import type { GitLabConfigInput, GitLabConfigView } from '~/composables/useAdmin'
import type { ProviderCardRow } from './ProviderCard.vue'

const props = defineProps<{
  config: GitLabConfigView | null
  submitting?: boolean
  deleting?: boolean
}>()

const emit = defineEmits<{
  save: [config: GitLabConfigInput]
  delete: []
}>()

const { t } = useI18n()

const appSettingsUrl = computed(() => {
  const url = props.config?.instance_url
  if (!url) return ''
  // User-owned apps live under /-/user_settings/applications.
  return `${url.replace(/\/$/, '')}/-/user_settings/applications`
})

const rows = computed<ProviderCardRow[]>(() => [
  { label: 'Instance URL', value: props.config?.instance_url },
  { label: 'Application ID', value: props.config?.client_id },
  { label: 'Client secret', masked: true },
  { label: 'Webhook secret', value: props.config?.webhook_secret, masked: !!props.config?.webhook_secret },
])
</script>

<template>
  <SectionShell
    title="GitLab OAuth"
    :description="t('admin.gitlab.description')"
  >
    <template v-if="config">
      <ProviderCard
        title="GitLab OAuth"
        icon="i-simple-icons-gitlab"
        icon-bg-class="bg-[#fc6d26]"
        :status-label="t('admin.configured')"
        :external-url="appSettingsUrl"
        :external-label="t('admin.gitlab.openOnGitlab')"
        :rows="rows"
      />
      <div class="mt-4">
        <DeleteProviderAction
          :button-label="t('admin.deleteGitlab')"
          :confirm-message="t('admin.confirmDeleteGitlab')"
          :confirm-button-label="t('admin.confirmDeleteButton')"
          :cancel-label="t('admin.cancel')"
          :loading="deleting"
          @confirm="emit('delete')"
        />
      </div>
    </template>

    <GitLabForm
      v-else
      :existing="null"
      :submitting="submitting"
      @submit="(c) => emit('save', c)"
    />
  </SectionShell>
</template>
