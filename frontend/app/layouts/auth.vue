<script setup lang="ts">
/**
 * Auth Layout
 *
 * Split-panel layout for login/signup pages.
 * Desktop: dark brand panel on left, form on right.
 * Mobile: single column centered layout.
 */
const colorMode = useColorMode()

function toggleDarkMode() {
  colorMode.preference = colorMode.value === 'dark' ? 'light' : 'dark'
}

const isDark = computed(() => colorMode.value === 'dark')
</script>

<template>
  <div class="min-h-screen flex flex-col lg:flex-row">
    <!-- Brand Panel (desktop only) -->
    <div class="hidden lg:flex lg:w-1/2 bg-neutral-900 dark:bg-neutral-950 relative overflow-hidden">
      <!-- Subtle grid pattern -->
      <div
        class="absolute inset-0 opacity-[0.03]"
        style="background-image: url('data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 width=%2240%22 height=%2240%22><line x1=%220%22 y1=%2240%22 x2=%2240%22 y2=%2240%22 stroke=%22white%22 stroke-width=%221%22/><line x1=%2240%22 y1=%220%22 x2=%2240%22 y2=%2240%22 stroke=%22white%22 stroke-width=%221%22/></svg>'); background-size: 40px 40px;"
      />

      <div class="relative z-10 flex flex-col justify-center px-16 xl:px-24">
        <LogoMark class="size-24 text-white/80 mb-8" />
        <h2 class="text-3xl xl:text-4xl font-semibold text-white tracking-tight leading-tight mb-4">
          Autonomous Sentry error triage, delivered in minutes.
        </h2>
        <p class="text-neutral-400 text-base max-w-md">
          Listens to your Sentry errors, triages them, and opens PRs to fix bugs automatically.
        </p>
      </div>
    </div>

    <!-- Form Panel -->
    <div class="flex-1 flex flex-col dither-bg dark:bg-neutral-950">
      <!-- Header -->
      <header class="h-14 flex items-center justify-between px-6 bg-white/80 dark:bg-neutral-900/80 backdrop-blur-sm border-b border-neutral-200 dark:border-neutral-700">
        <NuxtLink
          to="/"
          class="flex items-center gap-2"
        >
          <LogoMark class="size-6 text-neutral-700 dark:text-neutral-200" />
          <span class="text-sm font-semibold text-neutral-900 dark:text-neutral-100">{{ $t('common.appName') }}</span>
        </NuxtLink>

        <div class="flex items-center gap-2">
          <!-- Dark Mode Toggle - ClientOnly because colorMode differs between server/client -->
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
        </div>
      </header>

      <!-- Main Content -->
      <main class="flex-1 flex items-center justify-center p-6">
        <slot />
      </main>

      <!-- Footer -->
      <footer class="py-4 text-center text-xs text-neutral-400 dark:text-neutral-500">
        &copy; {{ new Date().getFullYear() }} Jeanclode. All rights reserved.
      </footer>
    </div>
  </div>
</template>
