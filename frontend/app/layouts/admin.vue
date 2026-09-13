<script setup lang="ts">
/**
 * Admin Layout
 *
 * Standalone layout for ADMIN_SECRET-gated pages (/setup, /admin).
 * No sidebar, no workspace switcher — admin settings are instance-wide,
 * not scoped to a user session.
 */
const colorMode = useColorMode()

function toggleDarkMode() {
  colorMode.preference = colorMode.value === 'dark' ? 'light' : 'dark'
}

const isDark = computed(() => colorMode.value === 'dark')
</script>

<template>
  <div class="min-h-screen flex flex-col bg-neutral-50 dark:bg-neutral-950">
    <header class="h-14 flex items-center justify-between px-6 bg-white dark:bg-neutral-900 border-b border-neutral-200 dark:border-neutral-700">
      <NuxtLink
        to="/"
        class="flex items-center gap-2"
      >
        <LogoMark class="size-6 text-neutral-700 dark:text-neutral-200" />
        <span class="text-sm font-semibold text-neutral-900 dark:text-neutral-100">
          {{ $t('common.appName') }}
        </span>
      </NuxtLink>

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

    <main class="flex-1">
      <slot />
    </main>
  </div>
</template>
