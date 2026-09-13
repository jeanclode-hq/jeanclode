<script setup lang="ts">
// only the crossing matters, so the ref flips twice per page rather than on every scroll event
const scrolled = ref(false)
function onScroll() {
  const past = window.scrollY > 400
  if (past !== scrolled.value) scrolled.value = past
}
onMounted(() => window.addEventListener('scroll', onScroll, { passive: true }))
onUnmounted(() => window.removeEventListener('scroll', onScroll))

function scrollToTop() {
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

const navItems = [
  { label: 'Docs', to: '/docs' },
  { label: 'Blog', to: '/blog' },
  { label: 'Pricing', to: '/pricing' },
  { label: 'GitHub', to: 'https://github.com/jeanclode-hq/jeanclode', target: '_blank' },
]
</script>

<template>
  <div class="flex min-h-screen flex-col overflow-x-clip bg-[var(--ui-bg)]">
    <UHeader to="/" mode="slideover">
      <template #title>
        <img src="/logo.svg" alt="JeanClode" class="h-6 w-6" />
        <span class="text-sm font-semibold tracking-tight text-[var(--ui-text-highlighted)]">JeanClode</span>
      </template>

      <UNavigationMenu :items="navItems" variant="link" />

      <template #right>
        <UButton
          label="Get Started"
          color="neutral"
          variant="solid"
          to="https://github.com/jeanclode-hq/jeanclode"
          target="_blank"
        />
      </template>

      <template #body>
        <UNavigationMenu :items="navItems" orientation="vertical" class="w-full" />
      </template>
    </UHeader>

    <div class="flex flex-1 flex-col">
      <slot />
    </div>

    <!-- Back to top -->
    <Transition
      enter-active-class="transition duration-200 ease-out"
      enter-from-class="opacity-0 translate-y-2"
      enter-to-class="opacity-100 translate-y-0"
      leave-active-class="transition duration-150 ease-in"
      leave-from-class="opacity-100 translate-y-0"
      leave-to-class="opacity-0 translate-y-2"
    >
      <button
        v-if="scrolled"
        aria-label="Back to top"
        class="fixed bottom-6 right-6 z-50 flex h-10 w-10 items-center justify-center rounded-full border border-[var(--ui-border)] bg-[var(--ui-bg)] shadow-md transition-shadow duration-150 hover:shadow-lg"
        @click="scrollToTop"
      >
        <svg class="h-4 w-4 text-[var(--ui-text-muted)]" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true">
          <path stroke-linecap="round" stroke-linejoin="round" d="M5 15l7-7 7 7" />
        </svg>
      </button>
    </Transition>

    <footer class="border-t border-(--ui-border-muted)">
      <div class="mx-auto flex max-w-7xl flex-col items-center justify-between gap-4 px-4 py-8 sm:flex-row sm:px-10">
        <div class="flex items-center gap-2">
          <img src="/logo.svg" alt="JeanClode" class="h-5 w-5" />
          <span class="text-sm text-[var(--ui-text-muted)]">© 2026 JeanClode</span>
        </div>
        <div class="flex items-center gap-6">
          <NuxtLink
            to="/privacy"
            class="text-sm text-[var(--ui-text-muted)] transition-colors hover:text-[var(--ui-text-highlighted)]"
          >
            Privacy
          </NuxtLink>
          <NuxtLink
            to="/terms"
            class="text-sm text-[var(--ui-text-muted)] transition-colors hover:text-[var(--ui-text-highlighted)]"
          >
            Terms
          </NuxtLink>
        </div>
      </div>
    </footer>
  </div>
</template>
