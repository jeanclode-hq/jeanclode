<template>
  <main class="flex flex-1 flex-col">
    <section class="mx-auto grid w-full max-w-[88rem] flex-1 items-center gap-10 px-6 py-12 md:min-h-[calc(100svh-4rem)] md:grid-cols-12 md:gap-12 md:px-10 md:py-16">
      <!-- Rows of strokes, one vermilion line item: the drawing tilts with the pointer like the one on the home page -->
      <div class="story-panel order-first md:order-last md:col-span-6 md:col-start-7" :style="panelStyle">
        <div class="relative aspect-[16/10] w-full overflow-hidden rounded-xl bg-[#f7f4ee]">
          <ClientOnly>
            <img v-if="touch" src="/ink/pricing-m.webp" alt="Rows of ink strokes with a single vermilion stroke running through them" width="800" height="500" fetchpriority="high" class="h-full w-full object-cover" />
            <InkCanvas v-else src="/ink/pricing.json" motion="flow" eager alt="Rows of ink strokes with a single vermilion stroke running through them" />
            <template #fallback><div class="h-full w-full bg-[#f7f4ee]"></div></template>
          </ClientOnly>
          <div class="pointer-events-none absolute inset-0 rounded-xl ring-1 ring-inset ring-black/10"></div>
        </div>
      </div>

      <div class="md:col-span-6 md:row-start-1">
        <motion.p
          :initial="{ opacity: 0, y: 10 }"
          :animate="{ opacity: 1, y: 0 }"
          :transition="{ duration: 0.5, ease }"
          class="mb-5 font-mono text-[11px] uppercase tracking-[0.18em] text-[var(--ui-text-dimmed)]"
        >
          Pricing
        </motion.p>
        <motion.h1
          :initial="{ opacity: 0, y: 16 }"
          :animate="{ opacity: 1, y: 0 }"
          :transition="{ duration: 0.65, delay: 0.06, ease }"
          class="text-[2.5rem] font-medium leading-[1] tracking-[-0.04em] text-[var(--ink)] sm:text-[3.5rem] md:text-[4.5rem]"
        >
          Free.<br />Self-hostable.<br />Yours.
        </motion.h1>
        <motion.p
          :initial="{ opacity: 0, y: 12 }"
          :animate="{ opacity: 1, y: 0 }"
          :transition="{ duration: 0.6, delay: 0.14, ease }"
          class="mt-6 max-w-md text-[15px] leading-relaxed text-[var(--ui-text-muted)] md:text-base"
        >
          JeanClode is open source and free to self-host. Run it on your own infrastructure:
          no seat fees, no usage caps, no vendor lock-in. One line item on your bill, and it is your LLM key.
        </motion.p>

        <motion.ul
          :initial="{ opacity: 0, y: 12 }"
          :animate="{ opacity: 1, y: 0 }"
          :transition="{ duration: 0.55, delay: 0.2, ease }"
          class="mt-10 max-w-md divide-y divide-[var(--ui-border)] border-y border-[var(--ui-border)]"
        >
          <li v-for="feature in features" :key="feature" class="flex items-center justify-between gap-6 py-3.5 text-[15px] text-[var(--ui-text-toned)]">
            {{ feature }}
            <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[var(--accent)]">included</span>
          </li>
        </motion.ul>
        <motion.div
          :initial="{ opacity: 0, y: 10 }"
          :animate="{ opacity: 1, y: 0 }"
          :transition="{ duration: 0.5, delay: 0.3, ease }"
          class="mt-8 flex flex-wrap gap-3"
        >
          <UButton size="lg" color="neutral" variant="solid" trailing-icon="i-lucide-arrow-right" to="/docs/deployment/docker-compose">
            Self-host now
          </UButton>
          <UButton size="lg" color="neutral" variant="ghost" to="https://github.com/jeanclode-hq/jeanclode" target="_blank">
            <template #leading><img src="/github.svg" alt="" class="size-4" /></template>
            View on GitHub
          </UButton>
        </motion.div>
      </div>
    </section>
  </main>
</template>

<script setup lang="ts">
import { motion } from 'motion-v'
import { useMediaQuery, useMouse, usePreferredReducedMotion, useWindowSize } from '@vueuse/core'

useHead({
  link: [
    { rel: 'preload', href: '/ink/pricing.json', as: 'fetch', crossorigin: 'anonymous', media: '(hover: hover) and (pointer: fine)' },
    { rel: 'preload', href: '/ink/pricing-m.webp', as: 'image', fetchpriority: 'high', media: '(hover: none) and (pointer: coarse)' },
  ],
})

usePageSeo({
  title: 'Pricing — Free and Self-Hostable | JeanClode',
  description: 'JeanClode is open source and free to self-host: no seat fees, no usage caps, no vendor lock-in. The only line item on your bill is your own LLM key.',
})

const ease = [0.16, 1, 0.3, 1] as const

// touch devices get the still; the live drawing needs a pointer to react to anyway
const touch = useMediaQuery('(hover: none) and (pointer: coarse)')
const { x: mx, y: my, sourceType } = useMouse({ touch: false })
const { width: vw, height: vh } = useWindowSize()
const reduced = usePreferredReducedMotion()
const still = computed(() => reduced.value === 'reduce' || !sourceType.value || vw.value === 0 || vh.value === 0)
const px = computed(() => (still.value ? 0 : (mx.value / vw.value) * 2 - 1))
const py = computed(() => (still.value ? 0 : (my.value / vh.value) * 2 - 1))
const panelStyle = computed(() => ({
  transform: `translate3d(${px.value * 10}px, ${py.value * 6}px, 0)`,
}))

const features = [
  'Unlimited repositories',
  'Unlimited automated pull requests',
  'Full source code, audit everything',
  'Your data stays on your servers',
  'Community support via GitHub',
]
</script>

<style scoped>
.story-panel {
  transition: transform 0.7s cubic-bezier(0.16, 1, 0.3, 1);
  will-change: transform;
}
</style>
