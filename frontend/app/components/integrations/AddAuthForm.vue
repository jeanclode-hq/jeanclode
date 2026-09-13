<script setup lang="ts">
import type { AuthType, SubjectType } from '@jeanclode/api-types'

const props = defineProps<{
  orgId: string
  subjectType: SubjectType
  subjectId: string
}>()

const emit = defineEmits<{
  close: []
}>()

const { t } = useI18n()
const toast = useToast()

const { data: credential } = useCredentialQuery(props.subjectType, computed(() => props.subjectId))
const writeMutation = useWriteCredentialMutation()
const deleteMutation = useDeleteCredentialMutation()

const isSkill = computed(() => props.subjectType === 'plugin_installation')

// A skill has no home yet for a basic_auth/oauth2 secret's env var name
// (backend/api/plugins/container/dispatch_inputs.py:add_connectors_to_inputs
// skips those two with a warning) — only offer types that are actually
// wired end-to-end for a skill credential, so saving one never silently
// no-ops at dispatch time.
const allAuthTypes: { value: AuthType, label: string }[] = [
  { value: 'api_key', label: t('connectors.authType.apiKey') },
  { value: 'jwt', label: t('connectors.authType.jwt') },
  { value: 'basic_auth', label: t('connectors.authType.basicAuth') },
  { value: 'oauth2', label: t('connectors.authType.oauth2') },
]
const authTypes = computed(() => {
  if (!isSkill.value) return allAuthTypes
  const wired = allAuthTypes.filter((a) => a.value === 'api_key' || a.value === 'jwt')
  // A pre-existing credential saved before this restriction (or via a
  // direct API call) could still be basic_auth/oauth2 — keep it
  // selectable so the dropdown doesn't silently show a value it can't
  // display, rather than forcing a confusing reset on open.
  const existingType = credential.value?.auth_type
  if (existingType && !wired.some((a) => a.value === existingType)) {
    const existing = allAuthTypes.find((a) => a.value === existingType)
    if (existing) wired.push(existing)
  }
  return wired
})

const authType = ref<AuthType>(credential.value?.auth_type ?? 'api_key')

// api_key / jwt
const name = ref('')
const key = ref('')
const showAdvanced = ref(false)
const header = ref('Authorization')
const valuePrefix = ref('Bearer ')

// basic_auth
const username = ref('')
const password = ref('')

// oauth2 — restricted to the three grant types the proxy actually knows
// how to mint (security-proxy/proxy.py:_mint_oauth_token_sync), rather
// than free text that could silently 502 at request time. No
// authorization_code: that needs an interactive browser redirect, which
// has no fit in a proxy that mints tokens headlessly per execution.
const grantTypes: { value: string, label: string }[] = [
  { value: 'client_credentials', label: t('connectors.form.grantTypeClientCredentials') },
  { value: 'password', label: t('connectors.form.grantTypePassword') },
  { value: 'refresh_token', label: t('connectors.form.grantTypeRefreshToken') },
]
const clientId = ref('')
const clientSecret = ref('')
const tokenUrl = ref('')
const grantType = ref('client_credentials')
const scope = ref('')
// oauth2 password grant
const oauthUsername = ref('')
const oauthPassword = ref('')
// oauth2 refresh_token grant
const refreshToken = ref('')

// skill credentials need an explicit target host (no McpServer.host to fall back on)
const host = ref('')

// Prefill everything the GET response actually carries (auth_type + the
// non-secret settings) when editing an existing credential. The secret
// itself (key/password/client_secret/...) is never returned — PUT is a
// full replace by design (see write_credential's docstring) — so those
// fields always start blank and must be re-entered to save any change.
watch(
  credential,
  (c) => {
    if (!c) return
    authType.value = c.auth_type
    const s = (c.settings ?? {}) as Record<string, unknown>
    if (typeof s.header === 'string') header.value = s.header
    if (typeof s.value_prefix === 'string') valuePrefix.value = s.value_prefix
    if (typeof s.host === 'string') host.value = s.host
    if (typeof s.token_url === 'string') tokenUrl.value = s.token_url
    if (typeof s.grant_type === 'string') grantType.value = s.grant_type
    if (typeof s.scope === 'string') scope.value = s.scope
  },
  { immediate: true },
)

const isValid = computed(() => {
  if (isSkill.value && !host.value.trim()) return false
  if (authType.value === 'api_key' || authType.value === 'jwt') {
    if (isSkill.value && !name.value.trim()) return false
    return !!key.value.trim()
  }
  if (authType.value === 'basic_auth') {
    return !!username.value.trim() && !!password.value.trim()
  }
  if (!clientId.value.trim() || !clientSecret.value.trim() || !tokenUrl.value.trim()) return false
  if (grantType.value === 'password') return !!oauthUsername.value.trim() && !!oauthPassword.value.trim()
  if (grantType.value === 'refresh_token') return !!refreshToken.value.trim()
  return true
})

async function submit() {
  let secret: Record<string, unknown>
  let settings: Record<string, unknown>

  if (authType.value === 'api_key' || authType.value === 'jwt') {
    // ApiKeySecret.name is required by the backend regardless of subject
    // type, but it's only meaningful for a skill (it's the env var name
    // the skill script reads). For an mcp_server it's unused downstream —
    // fill in a stable placeholder rather than asking the user for a
    // value that means nothing to them.
    secret = { name: isSkill.value ? name.value : authType.value, key: key.value }
    settings = { header: header.value, value_prefix: valuePrefix.value }
  } else if (authType.value === 'basic_auth') {
    secret = { username: username.value, password: password.value }
    settings = {}
  } else {
    secret = { client_id: clientId.value, client_secret: clientSecret.value }
    if (grantType.value === 'password') {
      secret.username = oauthUsername.value
      secret.password = oauthPassword.value
    } else if (grantType.value === 'refresh_token') {
      secret.refresh_token = refreshToken.value
    }
    settings = { token_url: tokenUrl.value, grant_type: grantType.value, scope: scope.value || null }
  }
  if (isSkill.value) settings.host = host.value

  try {
    await writeMutation.mutateAsync({
      orgId: props.orgId,
      subjectType: props.subjectType,
      subjectId: props.subjectId,
      authType: authType.value,
      secret,
      settings,
    })
    toast.add({ title: t('connectors.toast.credentialSaved'), color: 'success' })
    emit('close')
  } catch (e) {
    toast.add({ title: t('connectors.toast.credentialSaveFailed'), description: extractApiError(e, t('connectors.toast.credentialSaveFailed')), color: 'error' })
  }
}

async function remove() {
  try {
    await deleteMutation.mutateAsync({
      orgId: props.orgId,
      subjectType: props.subjectType,
      subjectId: props.subjectId,
    })
    toast.add({ title: t('connectors.toast.credentialRemoved'), color: 'success' })
    emit('close')
  } catch (e) {
    toast.add({ title: t('connectors.toast.credentialRemoveFailed'), description: extractApiError(e, t('connectors.toast.credentialRemoveFailed')), color: 'error' })
  }
}
</script>

<template>
  <div class="rounded-lg border border-neutral-200 dark:border-neutral-700 p-4 space-y-4 bg-neutral-50 dark:bg-neutral-900/50">
    <div class="flex items-center justify-between gap-2">
      <UFormField class="w-56">
        <template #label>
          <span class="inline-flex items-center gap-1">
            {{ t('connectors.form.authType') }}
            <UTooltip :text="t('connectors.form.authTypeHelp')">
              <UIcon
                name="i-lucide-info"
                class="size-3.5 text-neutral-400 hover:text-neutral-600 dark:hover:text-neutral-300 cursor-help transition-colors"
              />
            </UTooltip>
          </span>
        </template>
        <USelect
          v-model="authType"
          :items="authTypes"
          value-key="value"
          size="xs"
          class="w-full"
        />
      </UFormField>
      <UButton
        icon="i-lucide-x"
        size="xs"
        color="neutral"
        variant="ghost"
        @click="emit('close')"
      />
    </div>

    <p
      v-if="isSkill"
      class="text-xs text-neutral-500 dark:text-neutral-400 -mt-2"
    >
      {{ t('connectors.form.skillAuthTypesNote') }}
    </p>
    <p
      v-if="credential"
      class="text-xs text-neutral-500 dark:text-neutral-400 -mt-2"
    >
      {{ t('connectors.form.editingNote') }}
    </p>

    <template v-if="authType === 'api_key' || authType === 'jwt'">
      <UFormField
        v-if="isSkill"
        :label="t('connectors.form.envVarName')"
        :help="t('connectors.form.envVarNameHelp')"
        required
      >
        <UInput
          v-model="name"
          size="xs"
          class="w-full"
          placeholder="WIKI_API_KEY"
        />
      </UFormField>
      <UFormField
        :label="authType === 'jwt' ? t('connectors.authType.jwt') : t('connectors.form.token')"
        :help="authType === 'jwt' ? t('connectors.form.tokenHelpJwt') : t('connectors.form.tokenHelpApiKey')"
        required
      >
        <UInput
          v-model="key"
          size="xs"
          class="w-full"
          type="password"
          :placeholder="t('connectors.form.token')"
        />
      </UFormField>

      <div>
        <button
          type="button"
          class="text-xs text-neutral-500 dark:text-neutral-400 hover:text-neutral-700 dark:hover:text-neutral-200 flex items-center gap-1 cursor-pointer"
          @click="showAdvanced = !showAdvanced"
        >
          <UIcon
            name="i-lucide-chevron-right"
            class="size-3 transition-transform duration-150"
            :class="{ 'rotate-90': showAdvanced }"
          />
          {{ t('connectors.form.advanced') }}
        </button>

        <div
          v-if="showAdvanced"
          class="flex gap-2 mt-2"
        >
          <UFormField
            :label="t('connectors.form.header')"
            :help="t('connectors.form.headerHelp')"
            class="flex-1"
          >
            <UInput
              v-model="header"
              size="xs"
              class="w-full"
            />
          </UFormField>
          <UFormField
            :label="t('connectors.form.valuePrefix')"
            :help="t('connectors.form.valuePrefixHelp')"
            class="flex-1"
          >
            <UInput
              v-model="valuePrefix"
              size="xs"
              class="w-full"
            />
          </UFormField>
        </div>
      </div>
    </template>

    <template v-else-if="authType === 'basic_auth'">
      <UFormField
        :label="t('connectors.form.username')"
        :help="t('connectors.form.usernameHelp')"
        required
      >
        <UInput
          v-model="username"
          size="xs"
          class="w-full"
        />
      </UFormField>
      <UFormField
        :label="t('connectors.form.password')"
        :help="t('connectors.form.passwordHelp')"
        required
      >
        <UInput
          v-model="password"
          size="xs"
          class="w-full"
          type="password"
        />
      </UFormField>
    </template>

    <template v-else-if="authType === 'oauth2'">
      <UFormField
        :label="t('connectors.form.clientId')"
        :help="t('connectors.form.clientIdHelp')"
        required
      >
        <UInput
          v-model="clientId"
          size="xs"
          class="w-full"
        />
      </UFormField>
      <UFormField
        :label="t('connectors.form.clientSecret')"
        :help="t('connectors.form.clientSecretHelp')"
        required
      >
        <UInput
          v-model="clientSecret"
          size="xs"
          class="w-full"
          type="password"
        />
      </UFormField>
      <UFormField
        :label="t('connectors.form.tokenUrl')"
        :help="t('connectors.form.tokenUrlHelp')"
        required
      >
        <UInput
          v-model="tokenUrl"
          size="xs"
          class="w-full"
          placeholder="https://provider.example.com/oauth/token"
        />
      </UFormField>
      <div class="flex gap-2">
        <UFormField
          :label="t('connectors.form.grantType')"
          :help="t('connectors.form.grantTypeHelp')"
          class="flex-1"
        >
          <USelect
            v-model="grantType"
            :items="grantTypes"
            value-key="value"
            size="xs"
            class="w-full"
          />
        </UFormField>
        <UFormField
          :label="t('connectors.form.scope')"
          :help="t('connectors.form.scopeHelp')"
          class="flex-1"
        >
          <UInput
            v-model="scope"
            size="xs"
            class="w-full"
          />
        </UFormField>
      </div>

      <div
        v-if="grantType === 'password'"
        class="flex gap-2"
      >
        <UFormField
          :label="t('connectors.form.oauthUsername')"
          :help="t('connectors.form.oauthUsernameHelp')"
          class="flex-1"
          required
        >
          <UInput
            v-model="oauthUsername"
            size="xs"
            class="w-full"
          />
        </UFormField>
        <UFormField
          :label="t('connectors.form.oauthPassword')"
          :help="t('connectors.form.oauthPasswordHelp')"
          class="flex-1"
          required
        >
          <UInput
            v-model="oauthPassword"
            size="xs"
            class="w-full"
            type="password"
          />
        </UFormField>
      </div>

      <UFormField
        v-else-if="grantType === 'refresh_token'"
        :label="t('connectors.form.refreshToken')"
        :help="t('connectors.form.refreshTokenHelp')"
        required
      >
        <UInput
          v-model="refreshToken"
          size="xs"
          class="w-full"
          type="password"
        />
      </UFormField>
    </template>

    <UFormField
      v-if="isSkill"
      :label="t('connectors.form.host')"
      :help="t('connectors.form.hostHelp')"
      required
      class="pt-3 border-t border-neutral-200 dark:border-neutral-700"
    >
      <UInput
        v-model="host"
        size="xs"
        class="w-full"
        placeholder="wiki.example.com"
      />
    </UFormField>

    <div class="flex items-center justify-between gap-2 pt-1">
      <UButton
        v-if="credential"
        :label="t('connectors.remove')"
        size="xs"
        color="error"
        variant="soft"
        :loading="deleteMutation.isLoading.value"
        @click="remove"
      />
      <div v-else />
      <UButton
        :label="t('connectors.save')"
        size="xs"
        :disabled="!isValid"
        :loading="writeMutation.isLoading.value"
        @click="submit"
      />
    </div>
  </div>
</template>
