<script setup lang="ts">
/**
 * Admin settings page — ongoing CRUD over git providers and LLM config.
 *
 * Gated by ADMIN_SECRET via the admin cookie (not the user session), so
 * this page bypasses the global auth middleware. The actual per-provider
 * views live in `components/admin/{GitHub,GitLab,LLM}Section.vue`; this
 * page just handles auth, layout, and dispatching save/delete calls.
 */
import type {
  AdminSettings,
  GitHubConfigInput,
  GitLabConfigInput,
  LLMCredentialInput,
  LLMCredentialView,
} from '~/composables/useAdmin'

definePageMeta({ layout: 'admin' })

type Section = 'github' | 'gitlab' | 'llm'
type DeleteTarget = 'github' | 'gitlab' | { credentialId: string }

const { t } = useI18n()
const toast = useToast()
const admin = useAdmin()
const queryCache = useQueryCache()

const secret = ref('')
const authSubmitting = ref(false)
const submitting = ref(false)
const deletingCategory = ref<DeleteTarget | null>(null)
const reorderingLlm = ref(false)
const settings = ref<AdminSettings | null>(null)
const llmCredentials = ref<LLMCredentialView[]>([])
const loading = ref(false)
const section = ref<Section>('github')

const navItems = computed(() => [
  { key: 'github' as const, label: 'GitHub', icon: 'i-simple-icons-github', configured: !!settings.value?.github },
  { key: 'gitlab' as const, label: 'GitLab', icon: 'i-simple-icons-gitlab', configured: !!settings.value?.gitlab },
  { key: 'llm' as const, label: 'LLM', icon: 'i-lucide-sparkles', configured: llmCredentials.value.length > 0 },
])

const setupComplete = computed(
  () => !!(settings.value?.github || settings.value?.gitlab) && llmCredentials.value.length > 0,
)

async function loadSettings() {
  loading.value = true
  try {
    const [s, credentials] = await Promise.all([
      admin.getSettings(),
      admin.listLlmCredentials(),
    ])
    settings.value = s
    llmCredentials.value = credentials
  } catch {
    settings.value = null
    admin.logout()
  } finally {
    loading.value = false
  }
  // Let the global auth middleware re-evaluate setup_required on next nav.
  await queryCache.invalidateQueries({ key: ['app-config'] })
}

onMounted(async () => {
  // Restore from cookie if present — e.g. after a full reload from the
  // GitHub manifest redirect. Falls through to the password prompt if
  // the cookie is missing or expired.
  loading.value = true
  try {
    settings.value = await admin.restoreSession()
    if (settings.value) {
      llmCredentials.value = await admin.listLlmCredentials()
    }
  } finally {
    loading.value = false
  }
})

async function onAuth() {
  authSubmitting.value = true
  try {
    await admin.authenticate(secret.value)
    await loadSettings()
  } catch {
    toast.add({ title: t('admin.invalidSecret'), color: 'error' })
  } finally {
    authSubmitting.value = false
  }
}

async function onLogout() {
  await admin.logout()
  settings.value = null
}

async function onContinue() {
  // Full reload so the middleware re-reads a fresh /config response — avoids
  // racing on the setup_required flag that just changed.
  await queryCache.invalidateQueries({ key: ['app-config'] })
  window.location.href = '/login'
}

// --- Save handlers ---
// Each calls the matching admin update endpoint, flashes a toast, and
// reloads settings so the configured-state card appears.

type SaveFn = () => Promise<void>
async function runSave(fn: SaveFn, successKey: string) {
  submitting.value = true
  try {
    await fn()
    toast.add({ title: t(successKey), color: 'success' })
    await loadSettings()
  } catch {
    toast.add({ title: t('admin.saveFailed'), color: 'error' })
  } finally {
    submitting.value = false
  }
}

const onSaveGithub = (c: GitHubConfigInput) =>
  runSave(() => admin.updateGithub(c), 'admin.savedGithub')
const onSaveGitlab = (c: GitLabConfigInput) =>
  runSave(() => admin.updateGitlab(c), 'admin.savedGitlab')

async function onDelete(category: 'github' | 'gitlab') {
  deletingCategory.value = category
  try {
    await admin.deleteCategory(category)
    toast.add({ title: t('admin.deleted'), color: 'success' })
    await loadSettings()
  } catch {
    toast.add({ title: t('admin.saveFailed'), color: 'error' })
  } finally {
    deletingCategory.value = null
  }
}

// --- LLM credential pool handlers (ADR-010) ---

const onCreateLlm = (c: LLMCredentialInput) =>
  runSave(() => admin.createLlmCredential(c).then(() => {}), 'admin.savedLlm')

async function onUpdateLlm(id: string, c: LLMCredentialInput) {
  submitting.value = true
  try {
    await admin.updateLlmCredential(id, c)
    toast.add({ title: t('admin.savedLlm'), color: 'success' })
    llmCredentials.value = await admin.listLlmCredentials()
  } catch {
    toast.add({ title: t('admin.saveFailed'), color: 'error' })
  } finally {
    submitting.value = false
  }
}

async function onDeleteLlm(id: string) {
  deletingCategory.value = { credentialId: id }
  try {
    await admin.deleteLlmCredential(id)
    toast.add({ title: t('admin.deleted'), color: 'success' })
    llmCredentials.value = await admin.listLlmCredentials()
    await queryCache.invalidateQueries({ key: ['app-config'] })
  } catch {
    toast.add({ title: t('admin.saveFailed'), color: 'error' })
  } finally {
    deletingCategory.value = null
  }
}

async function onReorderLlm(orderedIds: string[]) {
  reorderingLlm.value = true
  const previous = llmCredentials.value
  try {
    llmCredentials.value = await admin.reorderLlmCredentials(orderedIds)
  } catch {
    llmCredentials.value = previous
    toast.add({ title: t('admin.saveFailed'), color: 'error' })
  } finally {
    reorderingLlm.value = false
  }
}

const deletingLlmId = computed(() => {
  const target = deletingCategory.value
  return target && typeof target === 'object' ? target.credentialId : null
})
</script>

<template>
  <!-- Unauthenticated — centered password card -->
  <div
    v-if="!admin.authenticated.value"
    class="min-h-[calc(100vh-3.5rem)] flex items-center justify-center p-6"
  >
    <div class="w-full max-w-sm">
      <div class="text-center mb-6">
        <UIcon
          name="i-lucide-lock"
          class="size-10 text-neutral-400 mx-auto mb-3"
        />
        <h1 class="text-xl font-semibold text-neutral-900 dark:text-neutral-100">
          {{ $t('admin.title') }}
        </h1>
        <p class="mt-1 text-sm text-neutral-500">
          {{ $t('admin.gatePrompt') }}
        </p>
      </div>

      <form
        class="flex flex-col gap-4"
        @submit.prevent="onAuth"
      >
        <UFormField
          :label="$t('admin.secret')"
          required
        >
          <UInput
            v-model="secret"
            type="password"
            class="w-full"
            autofocus
            required
          />
        </UFormField>
        <UButton
          type="submit"
          :loading="authSubmitting"
          block
        >
          {{ $t('admin.unlock') }}
        </UButton>
      </form>
    </div>
  </div>

  <!-- Authenticated — sidebar + content -->
  <div
    v-else
    class="max-w-5xl mx-auto w-full py-8 px-4 sm:px-6"
  >
    <div class="mb-6 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div class="min-w-0">
        <h1 class="text-2xl font-semibold text-neutral-900 dark:text-neutral-100">
          {{ $t('admin.title') }}
        </h1>
        <p class="text-sm text-neutral-500 mt-1">
          {{ $t('admin.subtitle') }}
        </p>
      </div>
      <div class="flex items-center gap-2 shrink-0">
        <span
          v-if="!setupComplete"
          class="text-xs text-neutral-500 dark:text-neutral-400 mr-1 hidden sm:inline"
          :title="$t('admin.setupIncomplete.description')"
        >
          {{ $t('admin.setupIncomplete.title') }}
        </span>
        <UButton
          :disabled="!setupComplete"
          size="sm"
          trailing-icon="i-lucide-arrow-right"
          class="whitespace-nowrap"
          @click="onContinue"
        >
          {{ $t('admin.continue') }}
        </UButton>
        <UButton
          variant="ghost"
          size="sm"
          icon="i-lucide-log-out"
          class="whitespace-nowrap"
          @click="onLogout"
        >
          {{ $t('admin.logout') }}
        </UButton>
      </div>
    </div>

    <div
      v-if="loading"
      class="flex items-center justify-center py-20"
    >
      <UIcon
        name="i-lucide-loader-2"
        class="size-6 animate-spin text-neutral-400"
      />
    </div>

    <div
      v-else-if="settings"
      class="grid grid-cols-1 md:grid-cols-[200px_1fr] gap-6"
    >
      <!-- Vertical nav -->
      <PageNav
        :items="navItems.map(i => ({ id: i.key, label: i.label, icon: i.icon, dot: i.configured }))"
        :model-value="section"
        @update:model-value="section = $event as typeof section"
      />

      <!-- Section content -->
      <div class="bg-white dark:bg-neutral-900 border border-neutral-200 dark:border-neutral-700 rounded-lg p-6">
        <GitHubSection
          v-if="section === 'github'"
          :config="settings.github"
          :submitting="submitting"
          :deleting="deletingCategory === 'github'"
          @save="onSaveGithub"
          @delete="onDelete('github')"
        />
        <GitLabSection
          v-else-if="section === 'gitlab'"
          :config="settings.gitlab"
          :submitting="submitting"
          :deleting="deletingCategory === 'gitlab'"
          @save="onSaveGitlab"
          @delete="onDelete('gitlab')"
        />
        <LLMSection
          v-else
          :credentials="llmCredentials"
          :submitting="submitting"
          :deleting-id="deletingLlmId"
          :reordering="reorderingLlm"
          @create="onCreateLlm"
          @update="onUpdateLlm"
          @delete="onDeleteLlm"
          @reorder="onReorderLlm"
        />
      </div>
    </div>
  </div>
</template>
