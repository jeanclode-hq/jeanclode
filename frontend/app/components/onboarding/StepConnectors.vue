<script setup lang="ts">
import type { BackfillScope } from '@jeanclode/api-types'

const props = defineProps<{
  workspaceId: string
  gitOrgIds: string[]
}>()

const emit = defineEmits<{
  complete: []
  back: []
}>()

const onboarding = useOnboarding()
const toast = useToast()
const { t } = useI18n()
const finalizeMutation = useFinalizeOnboardingMutation()
const isFinishing = finalizeMutation.isLoading

// Sentry connect state
const showSentryForm = ref(false)
const sentryOrgSlug = ref('')
const sentryAuthToken = ref('')
const sentryClientSecret = ref('')
const sentryBaseUrl = ref('https://sentry.io')
const backfillScope = ref<BackfillScope>(DEFAULT_BACKFILL_SCOPE)
const scopeOptions = computed(() => backfillOptions(t))
const scopeHint = computed(() => backfillHint(t, backfillScope.value))
const sentryMutation = useLinkSentrySourceMutation()
const isConnectingSentry = sentryMutation.isLoading

// Connectors definition — add more here as the product grows
interface Connector {
  id: string
  name: string
  description: string
  icon: string
  iconBg: string
  iconColor: string
  connected: boolean
  comingSoon?: boolean
}

const connectors = computed<Connector[]>(() => [
  {
    id: 'sentry',
    name: 'Sentry',
    description: 'Auto-triage errors and open fix PRs',
    icon: 'sentry',
    iconBg: 'bg-neutral-100 dark:bg-neutral-800',
    iconColor: 'text-neutral-600 dark:text-neutral-300',
    connected: onboarding.sentryOrgIds.value.length > 0,
  },
  {
    id: 'linear',
    name: 'Linear',
    description: 'Auto-fix issues from your backlog',
    icon: 'i-lucide-square-kanban',
    iconBg: 'bg-blue-50 dark:bg-blue-950',
    iconColor: 'text-blue-500',
    connected: false,
    comingSoon: true,
  },
  {
    id: 'jira',
    name: 'Jira',
    description: 'Sync and auto-fix Jira issues',
    icon: 'i-lucide-ticket',
    iconBg: 'bg-sky-50 dark:bg-sky-950',
    iconColor: 'text-sky-500',
    connected: false,
    comingSoon: true,
  },
  {
    id: 'datadog',
    name: 'Datadog',
    description: 'Monitor and auto-fix production alerts',
    icon: 'i-lucide-activity',
    iconBg: 'bg-neutral-100 dark:bg-neutral-800',
    iconColor: 'text-neutral-600 dark:text-neutral-300',
    connected: false,
    comingSoon: true,
  },
])

async function handleSentryConnect() {
  if (!sentryAuthToken.value.trim()) return

  try {
    const result = await sentryMutation.mutateAsync({
      workspaceId: props.workspaceId,
      orgSlug: sentryOrgSlug.value.trim(),
      authToken: sentryAuthToken.value.trim(),
      clientSecret: sentryClientSecret.value.trim(),
      baseUrl: sentryBaseUrl.value,
      backfillScope: backfillScope.value,
    })
    onboarding.addSentryOrg(result.org_id)
    sentryOrgSlug.value = ''
    sentryAuthToken.value = ''
    sentryClientSecret.value = ''
    sentryBaseUrl.value = 'https://sentry.io'
    backfillScope.value = DEFAULT_BACKFILL_SCOPE
    showSentryForm.value = false
  } catch (e: unknown) {
    toast.add({ title: 'Connection failed', description: extractApiError(e, 'Failed to connect Sentry'), color: 'error' })
  }
}

function handleConnectorClick(connector: Connector) {
  if (connector.comingSoon || connector.connected) return

  if (connector.id === 'sentry') {
    showSentryForm.value = true
  }
}

async function handleFinish() {
  try {
    await finalizeMutation.mutateAsync(props.workspaceId)
    emit('complete')
  } catch (e: unknown) {
    toast.add({ title: 'Setup failed', description: extractApiError(e, 'Failed to finalize setup'), color: 'error' })
  }
}

function handleDisconnect(connectorId: string) {
  if (connectorId === 'sentry') {
    // Remove all sentry orgs from onboarding state
    for (const id of [...onboarding.sentryOrgIds.value]) {
      onboarding.removeSentryOrg(id)
    }
  }
}
</script>

<template>
  <div class="flex flex-col gap-5">
    <div class="text-center mb-2">
      <p class="text-sm text-neutral-600 dark:text-neutral-400">
        Choose which integrations to connect. You can always add more later in settings.
      </p>
    </div>

    <!-- Connector cards -->
    <div class="space-y-3">
      <div
        v-for="connector in connectors"
        :key="connector.id"
        class="rounded-xl border overflow-hidden transition-all"
        :class="[
          connector.connected
            ? 'border-green-200 dark:border-green-800 bg-green-50/30 dark:bg-green-950/20'
            : connector.comingSoon
              ? 'border-neutral-200 dark:border-neutral-700 opacity-60'
              : 'border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-900',
        ]"
      >
        <!-- Card header -->
        <div class="flex items-center gap-4 p-4">
          <div
            class="size-10 rounded-lg flex items-center justify-center shrink-0"
            :class="connector.iconBg"
          >
            <ProviderIcon
              v-if="!connector.icon.startsWith('i-')"
              :light-src="getProviderIcon(connector.icon).lightSrc"
              :dark-src="getProviderIcon(connector.icon).darkSrc"
              :fallback="getProviderIcon(connector.icon).fallback"
              class="size-5"
            />
            <UIcon
              v-else
              :name="connector.icon"
              class="size-5"
              :class="connector.iconColor"
            />
          </div>

          <div class="flex-1 min-w-0">
            <div class="flex items-center gap-2">
              <p class="text-sm font-semibold text-neutral-900 dark:text-neutral-100">
                {{ connector.name }}
              </p>
              <UBadge
                v-if="connector.comingSoon"
                label="Coming soon"
                color="neutral"
                variant="subtle"
                size="xs"
              />
            </div>
            <p class="text-xs text-neutral-500 dark:text-neutral-400 mt-0.5">
              {{ connector.description }}
            </p>
          </div>

          <!-- Status / action -->
          <div class="flex items-center gap-2 shrink-0">
            <template v-if="connector.connected">
              <span class="size-2 rounded-full bg-green-500" />
              <UButton
                icon="i-lucide-trash-2"
                variant="ghost"
                color="neutral"
                size="xs"
                @click="handleDisconnect(connector.id)"
              />
            </template>
            <UButton
              v-else-if="!connector.comingSoon"
              label="Connect"
              variant="outline"
              size="sm"
              @click="handleConnectorClick(connector)"
            />
          </div>
        </div>

        <!-- Sentry inline form -->
        <div
          v-if="connector.id === 'sentry' && showSentryForm && !connector.connected"
          class="border-t border-neutral-200 dark:border-neutral-700 p-4 bg-neutral-50/50 dark:bg-neutral-900/50"
        >
          <form
            class="space-y-3"
            @submit.prevent="handleSentryConnect"
          >
            <p class="text-xs text-neutral-500 dark:text-neutral-400">
              Create an internal integration on your Sentry organization and fill in the details below.
            </p>

            <UFormField
              label="Organization Slug"
              required
            >
              <UInput
                v-model="sentryOrgSlug"
                placeholder="my-org (from your Sentry URL)"
                autofocus
                class="w-full"
              />
            </UFormField>

            <UFormField
              label="Auth Token"
              required
            >
              <UInput
                v-model="sentryAuthToken"
                type="password"
                placeholder="sntrys_..."
                class="w-full"
              />
            </UFormField>

            <UFormField
              label="Client Secret"
              required
            >
              <UInput
                v-model="sentryClientSecret"
                type="password"
                placeholder="From the integration's credentials section"
                class="w-full"
              />
            </UFormField>

            <UFormField label="Sentry URL">
              <UInput
                v-model="sentryBaseUrl"
                placeholder="https://sentry.io"
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

            <div class="flex items-center justify-end gap-2">
              <UButton
                label="Cancel"
                variant="ghost"
                color="neutral"
                size="sm"
                @click="showSentryForm = false"
              />
              <UButton
                type="submit"
                label="Connect"
                :loading="isConnectingSentry"
                :disabled="!sentryOrgSlug.trim() || !sentryAuthToken.trim() || !sentryClientSecret.trim()"
                size="sm"
              />
            </div>
          </form>
        </div>
      </div>
    </div>

    <!-- Footer -->
    <div class="flex items-center justify-between pt-3 border-t border-neutral-100 dark:border-neutral-700">
      <UButton
        :label="$t('common.back')"
        icon="i-lucide-arrow-left"
        variant="ghost"
        color="neutral"
        size="sm"
        @click="emit('back')"
      />
      <UButton
        label="Finish setup"
        icon="i-lucide-check"
        :loading="isFinishing"
        @click="handleFinish"
      />
    </div>
  </div>
</template>
