<template>
  <section id="how-it-works" class="scroll-mt-24 relative">
    <div class="mx-auto max-w-[88rem] px-6 md:px-10">
      <motion.div
        :initial="{ opacity: 0, y: 20 }"
        :while-in-view="{ opacity: 1, y: 0 }"
        :viewport="{ once: true, margin: '0px' }"
        :transition="{ duration: 0.7, ease }"
        class="mt-10 grid gap-8 md:mt-14 md:grid-cols-12"
      >
        <h2 class="text-[2.25rem] font-medium leading-[1] tracking-[-0.04em] text-[var(--ink)] md:col-span-6 md:text-[3.25rem]">
          From event to merged.
        </h2>
        <p class="max-w-sm text-base leading-relaxed text-[var(--ui-text-muted)] md:col-span-4 md:col-start-9 md:self-end">
          Whatever the source, every event follows the same lifecycle: triage, decide, fix, verify,
          review. No step waits on a human.
        </p>
      </motion.div>
    </div>

    <!-- Desktop: the lifecycle plays out in a pinned stage as you scroll -->
    <div
      ref="stage"
      class="relative hidden lg:block"
      :style="{ height: `${steps.length * 80 + 30}vh` }"
    >
      <div class="sticky top-0 flex h-screen items-center overflow-hidden">
        <div class="pointer-events-none absolute inset-0" aria-hidden="true">
          <div
            class="hidden"
          ></div>
        </div>

        <div class="relative mx-auto grid w-full max-w-6xl grid-cols-[minmax(0,0.9fr)_minmax(0,1fr)] items-center gap-14 px-10 xl:gap-20">
          <div class="flex gap-8">
            <!-- Step rail -->
            <div class="relative flex shrink-0 flex-col justify-center py-1">
              <span class="absolute bottom-3 left-[5px] top-3 w-px bg-[var(--ui-border)]" aria-hidden="true"></span>
              <span
                class="absolute left-[5px] top-3 w-px bg-[var(--ink)] transition-[height] duration-300 ease-out"
                :style="{ height: `calc((100% - 24px) * ${railFill})` }"
                aria-hidden="true"
              ></span>
              <button
                v-for="(step, i) in steps"
                :key="step.number"
                type="button"
                class="group relative flex items-center gap-3.5 py-2.5 text-left"
                :aria-current="i === activeStep ? 'step' : undefined"
                @click="goTo(i)"
              >
                <span
                  class="relative z-10 flex size-[11px] items-center justify-center rounded-full border bg-[var(--ui-bg)] transition-colors duration-300"
                  :class="i <= activeStep ? 'border-[var(--ui-text-highlighted)]' : 'border-[var(--ui-border-accented)]'"
                >
                  <span
                    class="size-[5px] rounded-full transition-all duration-300"
                    :class="i === activeStep ? 'scale-100 bg-[var(--ui-text-highlighted)]' : i < activeStep ? 'scale-75 bg-[var(--ui-text-dimmed)]' : 'scale-0 bg-transparent'"
                  ></span>
                </span>
                <span
                  class="font-mono text-[11px] tracking-[0.18em] transition-colors duration-300"
                  :class="i === activeStep ? 'text-[var(--ink)]' : 'text-[var(--ui-text-dimmed)] group-hover:text-[var(--ui-text-muted)]'"
                >{{ step.number }}</span>
              </button>
            </div>

            <!-- Copy, one step at a time -->
            <div class="relative min-h-[340px] flex-1">
              <div
                v-for="(step, i) in steps"
                :key="step.number"
                class="absolute inset-x-0 top-0 transition-all duration-500 ease-out"
                :class="i === activeStep ? 'translate-y-0 opacity-100 blur-0' : 'pointer-events-none translate-y-4 opacity-0 blur-[2px]'"
                :aria-hidden="i !== activeStep"
              >
                <div class="mb-5 flex items-center gap-2.5 text-[var(--ui-text-muted)]">
                  <UIcon :name="step.icon" class="size-4" />
                  <span class="font-mono text-[11px] uppercase tracking-[0.18em]">step {{ step.number }}</span>
                </div>
                <h3 class="mb-4 text-[2.5rem] font-medium leading-[1.02] tracking-[-0.035em] text-[var(--ink)] xl:text-[3rem]">
                  {{ step.title }}
                </h3>
                <ul v-if="Array.isArray(step.body)" class="max-w-md space-y-2">
                  <li
                    v-for="line in step.body"
                    :key="line"
                    class="flex items-start gap-2.5 text-[1.0625rem] leading-relaxed text-[var(--ui-text-muted)]"
                  >
                    <span class="mt-[0.65em] size-1 shrink-0 rounded-full bg-[var(--ui-text-dimmed)]"></span>
                    <span>{{ line }}</span>
                  </li>
                </ul>
                <p v-else class="max-w-md text-[1.0625rem] leading-relaxed text-[var(--ui-text-muted)]">
                  {{ step.body }}
                </p>
              </div>
            </div>
          </div>

          <!-- Stage -->
          <div class="relative aspect-[4/3] w-full overflow-hidden rounded-lg border border-[var(--ui-border)] bg-[var(--ui-bg)]">
            <div class="stage-grid pointer-events-none absolute inset-0" aria-hidden="true"></div>
            <div class="absolute inset-x-0 top-0 flex items-center justify-between px-5 py-3.5">
              <span class="font-mono text-[10px] tracking-[0.18em] text-[var(--ui-text-dimmed)]">LIFECYCLE</span>
              <span class="font-mono text-[10px] text-[var(--ui-text-dimmed)]">{{ steps[activeStep]?.number }} / {{ steps.length }}</span>
            </div>

            <div
              v-for="(step, i) in steps"
              :key="step.number"
              class="absolute inset-0 flex items-center justify-center px-8 pt-8 transition-all duration-500 ease-out"
              :class="i === activeStep ? 'scale-100 opacity-100' : 'pointer-events-none scale-[0.97] opacity-0'"
              :aria-hidden="i !== activeStep"
            >
              <HowItWorksVisual :step="i" :active="i === activeStep" />
            </div>

            <div class="absolute inset-x-0 bottom-0 h-px bg-[var(--ui-border)]">
              <div
                class="h-full bg-[var(--ui-text-highlighted)] transition-[width] duration-200 ease-linear"
                :style="{ width: `${progress * 100}%` }"
              ></div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Mobile / tablet: the same beats, stacked -->
    <div ref="mobileList" class="mx-auto max-w-2xl px-6 pb-8 pt-12 lg:hidden">
      <div
        v-for="(step, i) in steps"
        :key="step.number"
        :data-step="i"
        class="relative border-l border-[var(--ui-border)] pb-12 pl-6 last:pb-0"
      >
        <span
          class="absolute -left-[3.5px] top-1.5 size-[7px] rounded-full transition-colors duration-500"
          :class="visible.has(i) ? 'bg-[var(--ui-text-highlighted)]' : 'bg-[var(--ui-border-accented)]'"
        ></span>
        <div class="mb-3 flex items-center gap-2.5 text-[var(--ui-text-muted)]">
          <UIcon :name="step.icon" class="size-3.5" />
          <span class="font-mono text-[10px] uppercase tracking-[0.18em]">step {{ step.number }}</span>
        </div>
        <h3 class="mb-2.5 text-2xl font-medium leading-tight tracking-[-0.03em] text-[var(--ink)]">
          {{ step.title }}
        </h3>
        <ul v-if="Array.isArray(step.body)" class="space-y-1.5">
          <li
            v-for="line in step.body"
            :key="line"
            class="flex items-start gap-2 text-[0.95rem] leading-relaxed text-[var(--ui-text-muted)]"
          >
            <span class="mt-[0.65em] size-1 shrink-0 rounded-full bg-[var(--ui-text-dimmed)]"></span>
            <span>{{ line }}</span>
          </li>
        </ul>
        <p v-else class="text-[0.95rem] leading-relaxed text-[var(--ui-text-muted)]">{{ step.body }}</p>

        <div class="mt-5 overflow-hidden rounded-lg border border-[var(--ui-border)] bg-[var(--ui-bg)] px-6 py-8">
          <HowItWorksVisual :step="i" :active="visible.has(i)" />
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { motion } from 'motion-v'
import { useElementBounding, useIntersectionObserver, useWindowSize } from '@vueuse/core'

const ease = [0.16, 1, 0.3, 1] as const

const steps = [
  {
    number: '01',
    icon: 'i-lucide-radio-tower',
    title: 'Event arrives',
    body: 'Sentry error, GitHub issue, GitLab MR, a comment mentioning @jeanclode-bot: JeanClode listens to every integration you connect.',
  },
  {
    number: '02',
    icon: 'i-lucide-scan-search',
    title: 'Triage',
    body: 'The triage agent reads the issue, stack trace, and codebase. Real bug, infrastructure noise, or needs clarification, classified in seconds.',
  },
  {
    number: '03',
    icon: 'i-lucide-git-branch',
    title: 'Decision',
    body: [
      'Actionable → fix pipeline.',
      'Noise → skip.',
      'Unclear → ask for context.',
      'PR already exists → final, never retried.',
    ],
  },
  {
    number: '04',
    icon: 'i-lucide-wrench',
    title: 'Fix',
    body: 'Related issues are grouped by root cause. Triage\'s findings map straight to a fix, and every repo the fix touches gets its own PR on one shared branch.',
  },
  {
    number: '05',
    icon: 'i-lucide-shield-check',
    title: 'Verify',
    body: 'The fix has to pass your repo\'s own CI before the agent is allowed to finish. A failure comes back with the logs, and it tries again.',
  },
  {
    number: '06',
    icon: 'i-lucide-refresh-cw',
    title: 'Review loop',
    body: 'Seven agents review the fix. The bot works through every finding and pushes, which sends the PR back through review. When a pass comes back clean, your team gets tagged.',
  },
]

const stage = ref<HTMLElement | null>(null)
const { top, height } = useElementBounding(stage)
const { height: viewport } = useWindowSize()

const progress = computed(() => {
  const travel = height.value - viewport.value
  if (travel <= 0) return 0
  return Math.min(Math.max(-top.value / travel, 0), 1)
})

const scaled = computed(() => progress.value * steps.length)
const activeStep = computed(() => Math.min(Math.floor(scaled.value), steps.length - 1))
const stepProgress = computed(() => Math.min(scaled.value - activeStep.value, 1))
const railFill = computed(() => Math.min((activeStep.value + stepProgress.value) / (steps.length - 1), 1))

function goTo(i: number) {
  const el = stage.value
  if (!el) return
  const travel = el.offsetHeight - window.innerHeight
  const start = el.getBoundingClientRect().top + window.scrollY
  // land mid-step so the beat reads as active rather than as a boundary
  window.scrollTo({ top: start + (travel * (i + 0.35)) / steps.length, behavior: 'smooth' })
}

// Mobile beats animate as they scroll into view
const mobileList = ref<HTMLElement | null>(null)
const visible = ref(new Set<number>())

onMounted(() => {
  const nodes = mobileList.value?.querySelectorAll<HTMLElement>('[data-step]')
  nodes?.forEach((node) => {
    const index = Number(node.dataset.step)
    const { stop } = useIntersectionObserver(
      node,
      ([entry]) => {
        if (!entry?.isIntersecting) return
        visible.value = new Set(visible.value).add(index)
        stop()
      },
      { threshold: 0.35 },
    )
  })
})
</script>

<style scoped>
.stage-grid {
  background-image:
    linear-gradient(to right, var(--ui-border) 1px, transparent 1px),
    linear-gradient(to bottom, var(--ui-border) 1px, transparent 1px);
  background-size: 44px 44px;
  opacity: 0.35;
  mask-image: radial-gradient(ellipse 75% 65% at 50% 45%, black 20%, transparent 80%);
}
</style>
