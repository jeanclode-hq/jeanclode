<script setup lang="ts">
import type { NuxtError } from '#app'

const props = defineProps<{ error: NuxtError }>()

const isNotFound = computed(() => props.error?.statusCode === 404)

const heading = computed(() => isNotFound.value ? 'Page not found' : 'Something broke on our end')
const message = computed(() =>
  isNotFound.value
    ? "The page you're looking for doesn't exist or has moved."
    : "An unexpected error interrupted this page. It's on us, not you — try again in a moment.",
)

useSeoMeta({
  title: `${props.error?.statusCode ?? 'Error'} | JeanClode`,
  robots: 'noindex, follow',
})

function goHome() {
  clearError({ redirect: '/' })
}

function retry() {
  window.location.reload()
}
</script>

<template>
  <NuxtLayout name="default">
    <main class="flex flex-1 flex-col items-center justify-center px-6 py-24 text-center">
      <span class="mb-4 font-mono text-sm font-medium uppercase tracking-widest text-[var(--ui-text-muted)]">
        Error {{ error?.statusCode ?? '' }}
      </span>
      <h1 class="mb-4 text-[2rem] font-semibold tracking-tight text-[var(--ui-text-highlighted)] sm:text-[2.5rem]">
        {{ heading }}
      </h1>
      <p class="mb-8 max-w-md text-base leading-relaxed text-[var(--ui-text-muted)]">
        {{ message }}
      </p>
      <div class="flex flex-wrap items-center justify-center gap-3">
        <UButton size="lg" color="neutral" variant="solid" @click="goHome">
          Back to home
        </UButton>
        <UButton v-if="!isNotFound" size="lg" color="neutral" variant="ghost" @click="retry">
          Try again
        </UButton>
      </div>
    </main>
  </NuxtLayout>
</template>
