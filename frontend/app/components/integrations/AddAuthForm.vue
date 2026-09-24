<script setup lang="ts">
import type { AuthType, CredentialStatus, SubjectType } from '@jeanclode/api-types'

const props = defineProps<{
  orgId: string
  subjectType: SubjectType
  subjectId: string
  // An MCP server's own host, shown for its auths that don't name another
  defaultHost?: string
}>()

const emit = defineEmits<{
  close: []
}>()

const { t } = useI18n()
const toast = useToast()

const { data: credentials } = useCredentialsQuery(() => props.orgId, props.subjectType, () => props.subjectId)
const writeMutation = useWriteCredentialMutation()
const deleteMutation = useDeleteCredentialMutation()

const isSkill = computed(() => props.subjectType === 'plugin_installation')

// One tab per stored auth (a subject holds one per target host), plus one
// to add another.
const NEW_TAB = 'new'
const selected = ref<string>(NEW_TAB)
// Set once the user edits the new-auth tab, so a late credentials load
// doesn't pull them onto an existing auth mid-typing.
const touched = ref(false)
// A just-saved auth the refetched list may not contain yet.
const justSaved = ref<string | null>(null)
const current = computed<CredentialStatus | null>(
  () => credentials.value?.find((c) => c.id === selected.value) ?? null,
)
const tabs = computed(() => [
  ...(credentials.value ?? []).map((c) => ({ label: credentialLabel(t, c, props.defaultHost), value: c.id })),
  { label: t('connectors.form.newAuth'), icon: 'i-lucide-plus', value: NEW_TAB },
])

watch(
  credentials,
  (list) => {
    if (!list) return
    if (justSaved.value && list.some((c) => c.id === justSaved.value)) justSaved.value = null
    if (selected.value === justSaved.value) return
    if (selected.value === NEW_TAB && current.value === null && !touched.value && list.length) {
      selected.value = list[0]!.id
    } else if (selected.value !== NEW_TAB && !list.some((c) => c.id === selected.value)) {
      selected.value = list[0]?.id ?? NEW_TAB
    }
  },
  { immediate: true },
)

// oauth2 isn't wired for skills at dispatch time
// (backend/api/plugins/container/dispatch_inputs.py:_wire_skill_credential),
// so it's only offered for MCP servers — kept selectable on an existing
// skill credential that already has it, rather than showing a value the
// dropdown can't display.
const authTypes = computed(() => {
  const all: AuthType[] = ['api_key', 'jwt', 'basic_auth', 'oauth2', 'none']
  const offered = isSkill.value && current.value?.auth_type !== 'oauth2'
    ? all.filter((a) => a !== 'oauth2')
    : all
  return offered.map((value) => ({ value, label: authTypeLabel(t, value) }))
})

const authType = ref<AuthType>('api_key')

// api_key / jwt, and basic_auth's optional env var name on a skill
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

// Required on a skill (no McpServer.host to fall back on) and for `none`;
// optional on an MCP server, where it targets a host the server's tools call.
const host = ref('')
const hostRequired = computed(() => isSkill.value || authType.value === 'none')

// Prefill what the list carries (auth_type + the non-secret settings) for
// the selected auth. Secrets are never returned — PUT is a full replace
// (see write_credential's docstring) — so those always start blank.
watch(
  current,
  (c) => {
    name.value = ''
    key.value = ''
    username.value = ''
    password.value = ''
    clientId.value = ''
    clientSecret.value = ''
    oauthUsername.value = ''
    oauthPassword.value = ''
    refreshToken.value = ''
    showAdvanced.value = false
    const s = (c?.settings ?? {}) as Record<string, unknown>
    authType.value = c?.auth_type ?? 'api_key'
    header.value = typeof s.header === 'string' ? s.header : 'Authorization'
    valuePrefix.value = typeof s.value_prefix === 'string' ? s.value_prefix : 'Bearer '
    host.value = typeof s.host === 'string' ? s.host : ''
    tokenUrl.value = typeof s.token_url === 'string' ? s.token_url : ''
    grantType.value = typeof s.grant_type === 'string' ? s.grant_type : 'client_credentials'
    scope.value = typeof s.scope === 'string' ? s.scope : ''
  },
  { immediate: true },
)

const isValid = computed(() => {
  if (hostRequired.value && !host.value.trim()) return false
  if (authType.value === 'none') return true
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
  let secret: Record<string, unknown> = {}
  let settings: Record<string, unknown> = {}

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
    if (isSkill.value && name.value.trim()) secret.name = name.value.trim()
  } else if (authType.value === 'oauth2') {
    secret = { client_id: clientId.value, client_secret: clientSecret.value }
    if (grantType.value === 'password') {
      secret.username = oauthUsername.value
      secret.password = oauthPassword.value
    } else if (grantType.value === 'refresh_token') {
      secret.refresh_token = refreshToken.value
    }
    settings = { token_url: tokenUrl.value, grant_type: grantType.value, scope: scope.value || null }
  }
  if (host.value.trim()) settings.host = host.value.trim()

  try {
    const saved = await writeMutation.mutateAsync({
      orgId: props.orgId,
      subjectType: props.subjectType,
      subjectId: props.subjectId,
      credentialId: current.value?.id,
      authType: authType.value,
      secret,
      settings,
    })
    toast.add({ title: t('connectors.toast.credentialSaved'), color: 'success' })
    touched.value = false
    justSaved.value = saved.id
    selected.value = saved.id
  } catch (e) {
    toast.add({ title: t('connectors.toast.credentialSaveFailed'), description: extractApiError(e, t('connectors.toast.credentialSaveFailed')), color: 'error' })
  }
}

async function remove() {
  if (!current.value) return
  try {
    await deleteMutation.mutateAsync({
      orgId: props.orgId,
      subjectType: props.subjectType,
      subjectId: props.subjectId,
      credentialId: current.value.id,
    })
    toast.add({ title: t('connectors.toast.credentialRemoved'), color: 'success' })
  } catch (e) {
    toast.add({ title: t('connectors.toast.credentialRemoveFailed'), description: extractApiError(e, t('connectors.toast.credentialRemoveFailed')), color: 'error' })
  }
}
</script>

<template>
  <div
    class="rounded-lg border border-neutral-200 dark:border-neutral-700 p-4 space-y-4 bg-white dark:bg-neutral-800 shadow-xs dark:shadow-none"
    @input="touched = selected === NEW_TAB"
  >
    <div class="flex items-start justify-between gap-2">
      <UTabs
        v-if="credentials?.length"
        v-model="selected"
        :items="tabs"
        :content="false"
        variant="link"
        size="xs"
        class="min-w-0 flex-1"
      />
      <div v-else />
      <UButton
        icon="i-lucide-x"
        size="xs"
        color="neutral"
        variant="ghost"
        @click="emit('close')"
      />
    </div>

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

    <p
      v-if="isSkill"
      class="text-xs text-neutral-500 dark:text-neutral-400 -mt-2"
    >
      {{ t('connectors.form.skillAuthTypesNote') }}
    </p>
    <p
      v-if="current && authType !== 'none'"
      class="text-xs text-neutral-500 dark:text-neutral-400 -mt-2"
    >
      {{ t('connectors.form.editingNote') }}
    </p>

    <p
      v-if="authType === 'none'"
      class="text-xs text-neutral-500 dark:text-neutral-400"
    >
      {{ t('connectors.form.noneNote') }}
    </p>

    <template v-else-if="authType === 'api_key' || authType === 'jwt'">
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
        v-if="isSkill"
        :label="t('connectors.form.envVarNameOptional')"
        :help="t('connectors.form.envVarNameBasicHelp')"
      >
        <UInput
          v-model="name"
          size="xs"
          class="w-full"
          placeholder="WIKI_AUTH"
        />
      </UFormField>
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
      :label="t('connectors.form.host')"
      :help="isSkill ? t('connectors.form.hostHelp') : t('connectors.form.hostHelpMcp')"
      :required="hostRequired"
      class="pt-3 border-t border-neutral-200 dark:border-neutral-700"
    >
      <UInput
        v-model="host"
        size="xs"
        class="w-full"
        placeholder="api.example.com"
      />
    </UFormField>

    <div class="flex items-center justify-between gap-2 pt-1">
      <UButton
        v-if="current"
        :label="t('connectors.remove')"
        size="xs"
        color="error"
        variant="soft"
        :loading="deleteMutation.isLoading.value"
        @click="remove"
      />
      <div v-else />
      <UButton
        :label="current ? t('connectors.save') : t('connectors.form.addThisAuth')"
        size="xs"
        :disabled="!isValid"
        :loading="writeMutation.isLoading.value"
        @click="submit"
      />
    </div>
  </div>
</template>
