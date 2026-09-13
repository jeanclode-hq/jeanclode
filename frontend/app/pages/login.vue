<script setup lang="ts">
definePageMeta({
  layout: 'auth',
})

const { login } = useAuth()
const { data: config } = useAppConfigQuery()

const githubEnabled = computed(() => config.value?.providers.github_enabled ?? false)
const gitlabEnabled = computed(() => config.value?.providers.gitlab_enabled ?? false)
</script>

<template>
  <div class="w-full max-w-sm mx-auto">
    <div class="text-center mb-8">
      <h1 class="text-2xl font-semibold text-neutral-900 dark:text-neutral-100">
        {{ $t('auth.login.title') }}
      </h1>
      <p class="mt-2 text-sm text-neutral-500 dark:text-neutral-400">
        {{ $t('auth.login.subtitle') }}
      </p>
    </div>

    <div class="flex flex-col gap-3">
      <UButton
        v-if="githubEnabled"
        icon="i-simple-icons-github"
        size="lg"
        color="neutral"
        variant="outline"
        block
        @click="login('github')"
      >
        {{ $t('auth.continueWithGithub') }}
      </UButton>

      <UButton
        v-if="gitlabEnabled"
        icon="i-simple-icons-gitlab"
        size="lg"
        color="neutral"
        variant="outline"
        block
        @click="login('gitlab')"
      >
        {{ $t('auth.continueWithGitlab') }}
      </UButton>
    </div>
  </div>
</template>
