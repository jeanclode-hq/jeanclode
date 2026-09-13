<script setup lang="ts">
/**
 * Form for entering or updating GitHub App credentials.
 *
 * When `existing` is provided (admin mode), secret fields show "****" and
 * emit an empty string if unchanged — the parent should only PUT values
 * that actually changed.
 */
import type { GitHubConfigInput, GitHubConfigView } from '~/composables/useAdmin'

const props = defineProps<{
  existing?: GitHubConfigView | null
  submitting?: boolean
}>()

const emit = defineEmits<{
  submit: [config: GitHubConfigInput]
}>()

const form = reactive<GitHubConfigInput>({
  client_id: props.existing?.client_id ?? '',
  client_secret: '',
  app_id: props.existing?.app_id ?? '',
  private_key_pem: '',
  webhook_secret: '',
  name: props.existing?.name ?? 'jeanclode',
})

function onSubmit() {
  emit('submit', { ...form })
}
</script>

<template>
  <form
    class="flex flex-col gap-4"
    @submit.prevent="onSubmit"
  >
    <UFormField
      :label="$t('admin.github.name')"
      name="name"
      required
    >
      <UInput
        v-model="form.name"
        placeholder="jeanclode"
        class="w-full"
      />
    </UFormField>

    <UFormField
      :label="$t('admin.github.clientId')"
      name="client_id"
      required
    >
      <UInput
        v-model="form.client_id"
        placeholder="Iv1.abc123..."
        class="w-full"
      />
    </UFormField>

    <UFormField
      :label="$t('admin.github.clientSecret')"
      name="client_secret"
      :help="existing?.client_secret ? $t('admin.leaveBlankToKeep') : undefined"
    >
      <UInput
        v-model="form.client_secret"
        type="password"
        placeholder="••••••••"
        class="w-full"
        :required="!existing?.client_secret"
      />
    </UFormField>

    <UFormField
      :label="$t('admin.github.appId')"
      name="app_id"
      required
    >
      <UInput
        v-model="form.app_id"
        placeholder="123456"
        class="w-full"
      />
    </UFormField>

    <UFormField
      :label="$t('admin.github.privateKeyPem')"
      name="private_key_pem"
      :help="existing?.private_key_pem ? $t('admin.leaveBlankToKeep') : $t('admin.github.privateKeyHelp')"
    >
      <UTextarea
        v-model="form.private_key_pem"
        :rows="6"
        placeholder="-----BEGIN RSA PRIVATE KEY-----"
        class="w-full font-mono text-xs"
        :required="!existing?.private_key_pem"
      />
    </UFormField>

    <UFormField
      :label="$t('admin.github.webhookSecret')"
      name="webhook_secret"
      :help="existing?.webhook_secret ? $t('admin.leaveBlankToKeep') : undefined"
    >
      <UInput
        v-model="form.webhook_secret"
        type="password"
        placeholder="••••••••"
        class="w-full"
        :required="!existing?.webhook_secret"
      />
    </UFormField>

    <UButton
      type="submit"
      :loading="submitting"
      block
    >
      {{ $t('admin.save') }}
    </UButton>
  </form>
</template>
