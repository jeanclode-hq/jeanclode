<script setup lang="ts">
import type { NuxtError } from '#app'

const props = defineProps<{ error: NuxtError }>()

const { t } = useI18n()
const colorMode = useColorMode()

const isDark = computed(() => colorMode.value === 'dark')
function toggleDarkMode() {
  colorMode.preference = colorMode.value === 'dark' ? 'light' : 'dark'
}

const isNotFound = computed(() => props.error?.statusCode === 404)
const heading = computed(() => isNotFound.value ? t('errorPage.notFoundTitle') : t('errorPage.genericTitle'))
const message = computed(() => isNotFound.value ? t('errorPage.notFoundMessage') : t('errorPage.genericMessage'))

function goHome() {
  clearError({ redirect: '/' })
}

function retry() {
  window.location.reload()
}
</script>

<template>
  <NuxtLayout name="blank">
    <header class="h-14 flex items-center justify-between px-6 bg-white/80 dark:bg-neutral-900/80 backdrop-blur-sm border-b border-neutral-200 dark:border-neutral-700">
      <button
        type="button"
        class="flex items-center gap-2 cursor-pointer"
        @click="goHome"
      >
        <LogoMark class="size-6 text-neutral-700 dark:text-neutral-200" />
        <span class="text-sm font-semibold text-neutral-900 dark:text-neutral-100">{{ $t('common.appName') }}</span>
      </button>

      <ClientOnly>
        <UButton
          color="neutral"
          variant="ghost"
          size="xs"
          :icon="isDark ? 'i-lucide-moon' : 'i-lucide-sun'"
          :aria-label="isDark ? 'Switch to light mode' : 'Switch to dark mode'"
          @click="toggleDarkMode"
        />
      </ClientOnly>
    </header>

    <main class="flex-1 flex flex-col items-center justify-center px-6 py-24 text-center dither-bg dark:bg-neutral-950">
      <span class="mb-4 font-mono text-sm font-medium uppercase tracking-widest text-neutral-500 dark:text-neutral-400">
        {{ $t('common.error') }} {{ error?.statusCode ?? '' }}
      </span>
      <h1 class="mb-4 text-3xl font-semibold tracking-tight text-neutral-900 dark:text-neutral-100">
        {{ heading }}
      </h1>
      <p class="mb-8 max-w-md text-base leading-relaxed text-neutral-500 dark:text-neutral-400">
        {{ message }}
      </p>
      <div class="flex flex-wrap items-center justify-center gap-3">
        <UButton
          size="lg"
          color="neutral"
          variant="solid"
          @click="goHome"
        >
          {{ $t('errorPage.backToDashboard') }}
        </UButton>
        <UButton
          v-if="!isNotFound"
          size="lg"
          color="neutral"
          variant="outline"
          @click="retry"
        >
          {{ $t('errorPage.tryAgain') }}
        </UButton>
      </div>
    </main>

    <footer class="py-4 text-center text-xs text-neutral-400 dark:text-neutral-500">
      &copy; {{ new Date().getFullYear() }} Jeanclode. All rights reserved.
    </footer>
  </NuxtLayout>
</template>
