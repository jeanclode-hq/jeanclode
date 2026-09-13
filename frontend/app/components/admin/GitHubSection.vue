<script setup lang="ts">
/**
 * GitHub App admin section — configured-state card, manifest-flow form,
 * and "paste credentials" fallback tab.
 */
import type { GitHubConfigInput, GitHubConfigView } from '~/composables/useAdmin'
import type { ProviderCardRow } from './ProviderCard.vue'

const props = defineProps<{
  config: GitHubConfigView | null
  submitting?: boolean
  deleting?: boolean
}>()

const emit = defineEmits<{
  save: [config: GitHubConfigInput]
  delete: []
}>()

const { t } = useI18n()
const toast = useToast()
const admin = useAdmin()

const tab = ref<'manifest' | 'paste'>('manifest')
const manifestLoading = ref(false)
const manifestPublicUrl = ref('')
// GitHub App names are globally unique across GitHub — every self-hoster
// needs a distinct name. We enforce a ``jeanclode-`` prefix and let the
// admin choose the suffix so the resulting name is namespaced to their install.
const manifestAppNameSuffix = ref('')
const manifestAccountType = ref<'personal' | 'org'>('personal')
const manifestOrgSlug = ref('')

const manifestFullAppName = computed(
  () => `jeanclode-${manifestAppNameSuffix.value.trim()}`,
)

const canCreateApp = computed(() => {
  if (manifestLoading.value) return false
  if (!manifestAppNameSuffix.value.trim()) return false
  if (!manifestPublicUrl.value.trim()) return false
  if (manifestAccountType.value === 'org' && !manifestOrgSlug.value.trim()) return false
  return true
})

// When picking "An organization" and typing a slug, pre-fill the name
// suffix with that slug. Admin can override.
watch([manifestAccountType, manifestOrgSlug], ([type, slug]) => {
  if (type === 'org' && slug.trim() && !manifestAppNameSuffix.value.trim()) {
    manifestAppNameSuffix.value = slug.trim()
  }
})

const appSettingsUrl = computed(() => {
  const gh = props.config
  if (!gh?.name) return ''
  // Deep-link to Advanced tab where "Delete App" lives.
  if (gh.owner_type === 'Organization' && gh.owner_login) {
    return `https://github.com/organizations/${gh.owner_login}/settings/apps/${gh.name}/advanced`
  }
  return `https://github.com/settings/apps/${gh.name}/advanced`
})

const rows = computed<ProviderCardRow[]>(() => [
  { label: 'App ID', value: props.config?.app_id },
  { label: 'Client ID', value: props.config?.client_id },
  { label: 'Client secret', masked: true },
  { label: 'Private key', masked: true },
  { label: 'Webhook secret', masked: true },
])

async function startManifestFlow() {
  manifestLoading.value = true
  try {
    const { manifest, state, github_url } = await admin.getGithubManifest({
      publicUrl: manifestPublicUrl.value.trim() || undefined,
      name: manifestFullAppName.value,
      accountType: manifestAccountType.value,
      orgSlug:
        manifestAccountType.value === 'org'
          ? manifestOrgSlug.value.trim() || undefined
          : undefined,
    })
    // Post manifest to GitHub via a hidden form — GitHub creates the App
    // then redirects back to our /admin/github/manifest/callback endpoint.
    const form = document.createElement('form')
    form.method = 'POST'
    form.action = `${github_url}?state=${encodeURIComponent(state)}`
    const input = document.createElement('input')
    input.type = 'hidden'
    input.name = 'manifest'
    input.value = JSON.stringify(manifest)
    form.appendChild(input)
    document.body.appendChild(form)
    form.submit()
  } catch (err: unknown) {
    const detail = (err as { data?: { detail?: string } })?.data?.detail
    toast.add({ title: detail || t('admin.saveFailed'), color: 'error' })
    manifestLoading.value = false
  }
}
</script>

<template>
  <SectionShell
    title="GitHub App"
    :description="t('admin.github.description')"
  >
    <template v-if="config">
      <ProviderCard
        :title="config.name || 'GitHub App'"
        icon="i-simple-icons-github"
        icon-bg-class="bg-neutral-900 dark:bg-neutral-100"
        icon-text-class="text-white dark:text-neutral-900"
        :status-label="t('admin.configured')"
        :external-url="appSettingsUrl"
        :external-label="t('admin.github.openOnGithub')"
        :rows="rows"
      />

      <div class="mt-4 rounded-md border border-amber-200 dark:border-amber-900/40 bg-amber-50 dark:bg-amber-950/20 p-3 text-xs text-amber-900 dark:text-amber-200 flex gap-2">
        <UIcon
          name="i-lucide-info"
          class="size-4 shrink-0 mt-0.5"
        />
        <span>{{ t('admin.github.deleteWarning') }}</span>
      </div>

      <div class="mt-4">
        <DeleteProviderAction
          :button-label="t('admin.deleteGithub')"
          :confirm-message="t('admin.confirmDeleteGithub')"
          :confirm-button-label="t('admin.confirmDeleteButton')"
          :cancel-label="t('admin.cancel')"
          :loading="deleting"
          @confirm="emit('delete')"
        />
      </div>
    </template>

    <!-- Unconfigured: manifest-flow vs paste-credentials tabs -->
    <template v-else>
      <div class="flex gap-1 mb-5 p-1 rounded-md bg-neutral-100 dark:bg-neutral-800 w-fit">
        <button
          type="button"
          class="px-3 py-1.5 rounded text-sm transition-colors"
          :class="tab === 'manifest'
            ? 'bg-white dark:bg-neutral-600 text-neutral-900 dark:text-neutral-100 shadow-sm'
            : 'text-neutral-700 dark:text-neutral-400 hover:text-neutral-900 dark:hover:text-neutral-200'"
          @click="tab = 'manifest'"
        >
          {{ t('admin.github.tabs.manifest') }}
        </button>
        <button
          type="button"
          class="px-3 py-1.5 rounded text-sm transition-colors"
          :class="tab === 'paste'
            ? 'bg-white dark:bg-neutral-600 text-neutral-900 dark:text-neutral-100 shadow-sm'
            : 'text-neutral-700 dark:text-neutral-400 hover:text-neutral-900 dark:hover:text-neutral-200'"
          @click="tab = 'paste'"
        >
          {{ t('admin.github.tabs.paste') }}
        </button>
      </div>

      <div
        v-if="tab === 'manifest'"
        class="flex flex-col gap-4"
      >
        <p class="text-sm text-neutral-500">
          {{ t('admin.github.manifestHelp') }}
        </p>

        <UFormField
          :label="t('admin.github.name')"
          :help="t('admin.github.nameHelp')"
          required
        >
          <div class="flex items-stretch w-full">
            <span class="inline-flex items-center px-3 rounded-l-md border border-r-0 border-[var(--ui-border)] bg-[var(--ui-bg-muted)] text-[var(--ui-text-muted)] text-sm font-mono select-none">
              jeanclode-
            </span>
            <UInput
              v-model="manifestAppNameSuffix"
              :placeholder="manifestAccountType === 'org' ? 'my-org' : 'my-handle'"
              class="flex-1"
              :ui="{ base: 'rounded-l-none' }"
              required
            />
          </div>
        </UFormField>

        <UFormField :label="t('admin.github.accountTypeLabel')">
          <URadioGroup
            v-model="manifestAccountType"
            :items="[
              { label: t('admin.github.accountTypePersonal'), value: 'personal' },
              { label: t('admin.github.accountTypeOrg'), value: 'org' },
            ]"
          />
        </UFormField>

        <UFormField
          v-if="manifestAccountType === 'org'"
          :label="t('admin.github.orgSlugLabel')"
          :help="t('admin.github.orgSlugHelp')"
          required
        >
          <UInput
            v-model="manifestOrgSlug"
            placeholder="my-github-org"
            class="w-full"
            required
          />
        </UFormField>

        <UFormField
          :label="t('admin.github.publicUrlLabel')"
          :help="t('admin.github.publicUrlHelp')"
          required
        >
          <UInput
            v-model="manifestPublicUrl"
            placeholder="https://your-tunnel.ngrok-free.app"
            class="w-full"
            required
          />
        </UFormField>

        <UButton
          icon="i-simple-icons-github"
          :loading="manifestLoading"
          :disabled="!canCreateApp"
          @click="startManifestFlow"
        >
          {{ t('admin.github.createApp') }}
        </UButton>
      </div>

      <GitHubForm
        v-else
        :existing="null"
        :submitting="submitting"
        @submit="(c) => emit('save', c)"
      />
    </template>
  </SectionShell>
</template>
