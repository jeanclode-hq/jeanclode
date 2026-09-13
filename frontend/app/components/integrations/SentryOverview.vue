<script setup lang="ts">
import type { BackfillScope } from '@jeanclode/api-types'
import type { Organization } from '~/types/api'

const props = defineProps<{
  orgs: Organization[]
  workspaceId: string
}>()

const emit = defineEmits<{
  select: [orgId: string]
  refresh: []
}>()

const toast = useToast()
const { t } = useI18n()

// Connect form
const showConnectForm = ref(false)
const sentryOrgSlug = ref('')
const sentryAuthToken = ref('')
const sentryClientSecret = ref('')
const sentryBaseUrl = ref('https://sentry.io')
const sentryMutation = useLinkSentrySourceMutation()
const isConnecting = sentryMutation.isLoading

// Connecting an org queues a one-off import of its existing issues; this
// bounds how much of the backlog comes in.
const backfillScope = ref<BackfillScope>(DEFAULT_BACKFILL_SCOPE)
const scopeOptions = computed(() => backfillOptions(t))
const scopeHint = computed(() => backfillHint(t, backfillScope.value))

async function handleConnect() {
  if (!sentryOrgSlug.value.trim() || !sentryAuthToken.value.trim() || !sentryClientSecret.value.trim()) return
  try {
    await sentryMutation.mutateAsync({
      workspaceId: props.workspaceId,
      orgSlug: sentryOrgSlug.value.trim(),
      authToken: sentryAuthToken.value.trim(),
      clientSecret: sentryClientSecret.value.trim(),
      baseUrl: sentryBaseUrl.value,
      backfillScope: backfillScope.value,
    })
    sentryOrgSlug.value = ''
    sentryAuthToken.value = ''
    sentryClientSecret.value = ''
    sentryBaseUrl.value = 'https://sentry.io'
    backfillScope.value = DEFAULT_BACKFILL_SCOPE
    showConnectForm.value = false
    emit('refresh')
  } catch (e: unknown) {
    toast.add({ title: 'Connection failed', description: extractApiError(e, 'Failed to connect Sentry'), color: 'error' })
  }
}

function timeAgo(dateStr: string) {
  const diff = Date.now() - new Date(dateStr).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  return `${days}d ago`
}
</script>

<template>
  <div>
    <!-- Empty state -->
    <div
      v-if="orgs.length === 0 && !showConnectForm"
      class="flex flex-col items-center justify-center py-20 text-center animate-fade-up"
    >
      <div class="size-16 rounded-2xl bg-neutral-100 dark:bg-neutral-800 flex items-center justify-center mb-5">
        <ProviderIcon
          light-src="/icons/sentry-light.svg"
          dark-src="/icons/sentry-dark.svg"
          alt="Sentry"
          class="size-8"
        />
      </div>
      <h3 class="text-base font-semibold text-neutral-900 dark:text-neutral-100 mb-1.5">
        Connect a Sentry organization
      </h3>
      <p class="text-sm text-neutral-500 dark:text-neutral-400 max-w-sm mb-6">
        Link your Sentry org to automatically triage errors and dispatch fix agents to your codebase.
      </p>
      <UButton
        label="Connect Sentry"
        icon="i-lucide-plus"
        size="lg"
        @click="showConnectForm = true"
      />
    </div>

    <!-- Connected orgs + optional form -->
    <div
      v-else
      class="space-y-4"
    >
      <div
        v-if="orgs.length > 0"
        class="flex items-center justify-between"
      >
        <p class="text-xs font-medium text-neutral-500 dark:text-neutral-400 uppercase tracking-wider">
          {{ orgs.length }} organization{{ orgs.length !== 1 ? 's' : '' }} connected
        </p>
        <UButton
          label="Add organization"
          icon="i-lucide-plus"
          variant="ghost"
          color="neutral"
          size="xs"
          @click="showConnectForm = !showConnectForm"
        />
      </div>

      <!-- Connect form -->
      <div
        v-if="showConnectForm"
        class="rounded-xl border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 shadow-xs dark:shadow-none p-5"
      >
        <form
          class="space-y-4"
          @submit.prevent="handleConnect"
        >
          <p class="text-sm font-medium text-neutral-900 dark:text-neutral-100">
            Connect Sentry organization
          </p>
          <p class="text-xs text-neutral-500 dark:text-neutral-400 -mt-2">
            Create an internal integration on your Sentry org, then paste the credentials below.
          </p>

          <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <UFormField
              :label="$t('integrations.sentry.orgSlugLabel')"
              required
            >
              <UInput
                v-model="sentryOrgSlug"
                :placeholder="$t('integrations.sentry.orgSlugPlaceholder')"
                autofocus
                class="w-full"
              />
            </UFormField>
            <UFormField :label="$t('integrations.sentry.baseUrlLabel')">
              <UInput
                v-model="sentryBaseUrl"
                :placeholder="$t('integrations.sentry.baseUrlPlaceholder')"
                class="w-full"
              />
            </UFormField>
          </div>

          <UFormField
            :label="$t('integrations.sentry.authTokenLabel')"
            required
          >
            <UInput
              v-model="sentryAuthToken"
              type="password"
              :placeholder="$t('integrations.sentry.authTokenPlaceholder')"
              class="w-full"
            />
          </UFormField>

          <UFormField
            :label="$t('integrations.sentry.clientSecretLabel')"
            required
          >
            <UInput
              v-model="sentryClientSecret"
              type="password"
              :placeholder="$t('integrations.sentry.clientSecretPlaceholder')"
              class="w-full"
            />
          </UFormField>

          <UFormField :label="$t('onboarding.sentry.backfillLabel')">
            <USelect
              v-model="backfillScope"
              :items="scopeOptions"
              value-key="value"
              class="w-full"
            />
            <template #hint>
              <span class="text-[11px] text-neutral-500 dark:text-neutral-400">
                {{ $t('onboarding.sentry.backfillHint') }}
              </span>
            </template>
            <template #help>
              <span class="text-[11px] text-neutral-500 dark:text-neutral-400">
                {{ scopeHint }}
              </span>
            </template>
          </UFormField>

          <div class="flex items-center justify-end gap-2 pt-1">
            <UButton
              :label="$t('common.cancel')"
              variant="ghost"
              color="neutral"
              size="sm"
              @click="showConnectForm = false"
            />
            <UButton
              type="submit"
              label="Connect"
              size="sm"
              :loading="isConnecting"
              :disabled="!sentryOrgSlug.trim() || !sentryAuthToken.trim() || !sentryClientSecret.trim()"
            />
          </div>
        </form>
      </div>

      <!-- Org rows -->
      <div
        v-if="orgs.length > 0"
        class="rounded-xl border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 shadow-xs dark:shadow-none divide-y divide-neutral-200 dark:divide-neutral-700 overflow-hidden"
      >
        <button
          v-for="org in orgs"
          :key="org.id"
          type="button"
          class="cursor-pointer w-full flex items-center gap-4 px-4 py-3.5 text-left hover:bg-neutral-50 dark:hover:bg-neutral-700/50 transition-colors group"
          @click="emit('select', org.id)"
        >
          <div class="size-9 rounded-lg bg-neutral-100 dark:bg-neutral-700 flex items-center justify-center shrink-0">
            <ProviderIcon
              light-src="/icons/sentry-light.svg"
              dark-src="/icons/sentry-dark.svg"
              alt="Sentry"
              class="size-4.5"
            />
          </div>
          <div class="flex-1 min-w-0">
            <p class="text-sm font-medium text-neutral-900 dark:text-neutral-100 truncate">
              {{ org.name }}
            </p>
            <p class="text-xs text-neutral-500 mt-0.5 truncate">
              {{ org.repo_count }} {{ org.repo_count === 1 ? 'project' : 'projects' }}
              <span class="hidden sm:inline">&middot; {{ org.external_org_id }} &middot; Added {{ timeAgo(org.created_at) }}</span>
            </p>
          </div>
          <div class="flex items-center gap-2.5 shrink-0">
            <UBadge
              label="Connected"
              color="success"
              variant="subtle"
              size="xs"
            />
            <UIcon
              name="i-lucide-chevron-right"
              class="size-4 text-neutral-400 dark:text-neutral-600 group-hover:text-neutral-500 dark:group-hover:text-neutral-400 transition-colors"
            />
          </div>
        </button>
      </div>
    </div>
  </div>
</template>
