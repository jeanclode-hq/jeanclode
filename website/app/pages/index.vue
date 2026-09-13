<template>
  <main class="overflow-x-clip">
    <!-- ─── Stage: image story, scroll-driven. Pointer devices only — see .story-pinned ─── -->
    <section ref="stage" class="story-pinned relative" :style="{ height: `${beats.length * 150 + 160}vh` }">
      <div ref="pin" class="sticky top-16 h-[calc(100svh-4rem)] overflow-hidden">
        <!-- Artwork panel: each beat wipes in its own image as the scroll drives --r1…--r3, and the
             pointer tilt sits on its own layer so a scroll never restarts its 0.7s transition -->
        <div ref="panel" class="story-panel absolute inset-x-5 top-[9vh] aspect-[16/10] max-h-[clamp(0px,calc(100svh-34rem),42svh)] md:inset-x-auto md:right-[5vw] md:top-[11vh] md:max-h-[62svh] md:w-[56vw]">
          <div ref="tilt" class="story-tilt h-full w-full">
            <div class="story-frame relative h-full w-full overflow-hidden rounded-xl bg-[var(--ui-bg-muted)]">
              <ClientOnly>
                <!-- only mounted where the stage is actually shown, so a phone never builds the WebGL
                     renderer or fetches stroke data for a panel it will never see -->
                <InkCanvas v-if="pinned" :frames="inkFrames" :reveals="reveals" :active="inkActive" eager :alt="frames[activeBeat < 0 ? 0 : activeBeat]!.alt" />
                <template #fallback><div class="h-full w-full bg-[#f7f4ee]"></div></template>
              </ClientOnly>
              <div class="pointer-events-none absolute inset-0 overflow-hidden">
                <div class="story-light absolute -inset-1/2"></div>
              </div>
              <div class="pointer-events-none absolute inset-0 rounded-xl ring-1 ring-inset ring-black/10"></div>
            </div>
          </div>
        </div>

        <!-- Opening -->
        <div
          class="pointer-events-none absolute inset-x-0 bottom-0 px-6 pb-10 transition-[opacity,transform] duration-500 ease-out md:px-10 md:pb-14"
          :class="opening ? 'translate-y-0 opacity-100' : 'translate-y-3 opacity-0'"
        >
          <div class="mx-auto flex max-w-[88rem] flex-col gap-8 md:flex-row md:items-end md:justify-between">
            <div ref="copy" class="story-copy max-w-2xl" :class="opening ? 'pointer-events-auto' : ''">
              <h1 class="text-[2.5rem] font-medium leading-[1] tracking-[-0.04em] text-[var(--ink)] sm:text-[3.5rem] md:text-[4.5rem]">
                Autonomous engineering,<br />end to end.
              </h1>
              <p class="mt-6 max-w-md text-[15px] leading-relaxed text-[var(--ui-text-muted)] md:text-base">
                JeanClode watches your error tracker, issue tracker and code review, decides what deserves
                a fix, and ships CI-verified pull requests on its own. Open source. Runs on your infrastructure.
              </p>
              <div class="mt-8 flex flex-wrap items-center gap-3">
                <UButton size="lg" color="neutral" variant="solid" trailing-icon="i-lucide-arrow-right" to="/docs/getting-started/introduction">
                  Get started
                </UButton>
                <UButton size="lg" color="neutral" variant="ghost" to="https://github.com/jeanclode-hq/jeanclode" target="_blank">
                  <template #leading><img src="/github.svg" alt="" class="size-4" /></template>
                  GitHub
                </UButton>
              </div>
            </div>
            <p class="hidden font-mono text-[11px] uppercase tracking-[0.18em] text-[var(--ui-text-dimmed)] md:block">
              Scroll
            </p>
          </div>
        </div>

        <!-- Beats -->
        <div class="pointer-events-none absolute inset-x-0 bottom-0 px-6 pb-10 md:px-10 md:pb-14">
          <div class="mx-auto max-w-[88rem]">
            <div class="relative h-40 md:h-36">
              <div
                v-for="(beat, i) in beats"
                :key="beat.title"
                class="absolute inset-x-0 bottom-0 grid gap-6 transition-[opacity,transform] duration-500 ease-out md:grid-cols-12"
                :class="activeBeat === i ? 'translate-y-0 opacity-100' : activeBeat > i ? '-translate-y-3 opacity-0' : 'translate-y-3 opacity-0'"
                :aria-hidden="activeBeat !== i"
              >
                <p class="font-mono text-[11px] uppercase tracking-[0.18em] text-[var(--ui-text-dimmed)] md:col-span-2">
                  {{ String(i + 1).padStart(2, '0') }} / {{ String(beats.length).padStart(2, '0') }}
                </p>
                <h2 class="text-[2rem] font-medium leading-[1] tracking-[-0.04em] text-[var(--ink)] md:col-span-5 md:text-[3rem]">
                  {{ beat.title }}
                </h2>
                <p class="max-w-sm text-[15px] leading-relaxed text-[var(--ui-text-muted)] md:col-span-4 md:col-start-9 md:self-end">
                  {{ beat.body }}
                </p>
              </div>
            </div>
          </div>
        </div>

        <div class="absolute inset-x-0 bottom-0 h-px bg-[var(--ui-border)]">
          <div ref="bar" class="story-bar h-full w-full origin-left bg-[var(--ink)]"></div>
        </div>
      </div>
    </section>

    <!-- ─── The same story, stacked and scrolling normally ───
         Touch devices and narrow windows never get the pinned version. A stage pinned for seven
         screens of scrolling has nothing moving in it, so every retraction of the mobile URL bar
         drags the whole layer up and down against a still frame — the page cannot compensate for
         that, since the viewport it is positioned against is what moves. Scrolling content hides
         the same movement, which is why only this section ever shook. -->
    <section class="story-stacked">
      <div class="mx-auto max-w-[88rem] px-6 pb-16 pt-8 md:px-10">
        <h1 class="max-w-2xl text-[2.5rem] font-medium leading-[1] tracking-[-0.04em] text-[var(--ink)] sm:text-[3.5rem]">
          Autonomous engineering,<br />end to end.
        </h1>
        <p class="mt-6 max-w-md text-[15px] leading-relaxed text-[var(--ui-text-muted)]">
          JeanClode watches your error tracker, issue tracker and code review, decides what deserves
          a fix, and ships CI-verified pull requests on its own. Open source. Runs on your infrastructure.
        </p>
        <div class="mt-8 flex flex-wrap items-center gap-3">
          <UButton size="lg" color="neutral" variant="solid" trailing-icon="i-lucide-arrow-right" to="/docs/getting-started/introduction">
            Get started
          </UButton>
          <UButton size="lg" color="neutral" variant="ghost" to="https://github.com/jeanclode-hq/jeanclode" target="_blank">
            <template #leading><img src="/github.svg" alt="" class="size-4" /></template>
            GitHub
          </UButton>
        </div>

        <div class="mt-14 space-y-14">
          <article v-for="(beat, i) in beats" :key="beat.title">
            <div class="overflow-hidden rounded-xl bg-[#f7f4ee] ring-1 ring-inset ring-black/10">
              <!-- lazy across the board: the one that matters on a phone is preloaded in useHead,
                   and a desktop never paints this section at all -->
              <img
                :src="frames[i]!.still"
                :alt="frames[i]!.alt"
                width="800"
                height="500"
                loading="lazy"
                decoding="async"
                class="block aspect-[16/10] w-full object-cover"
              />
            </div>
            <p class="mt-5 font-mono text-[11px] uppercase tracking-[0.18em] text-[var(--ui-text-dimmed)]">
              {{ String(i + 1).padStart(2, '0') }} / {{ String(beats.length).padStart(2, '0') }}
            </p>
            <h2 class="mt-2 text-[2rem] font-medium leading-[1] tracking-[-0.04em] text-[var(--ink)]">
              {{ beat.title }}
            </h2>
            <p class="mt-3 max-w-md text-[15px] leading-relaxed text-[var(--ui-text-muted)]">
              {{ beat.body }}
            </p>
          </article>
        </div>
      </div>
    </section>

    <!-- ─── Workflows ─── -->
    <section class="scroll-mt-24 py-24 md:py-32" id="workflows">
      <div class="mx-auto max-w-[88rem] px-6 md:px-10">
        <div class="grid gap-8 md:grid-cols-12">
          <h2 class="text-[2rem] font-medium leading-[1.02] tracking-[-0.04em] text-[var(--ink)] md:col-span-5 md:text-[2.75rem]">
            One pipeline per kind of work.
          </h2>
          <p class="max-w-sm text-[15px] leading-relaxed text-[var(--ui-text-muted)] md:col-span-4 md:col-start-9 md:self-end">
            Each workflow is its own multi-agent pipeline on the Claude Agent SDK, with deterministic
            checks between phases. A reviewer never fixes. A fixer never merges.
          </p>
        </div>

        <div class="mt-16 border-t border-[var(--ui-border)] md:mt-20">
          <div class="hidden grid-cols-12 gap-8 py-3 font-mono text-[10.5px] uppercase tracking-[0.18em] text-[var(--ui-text-dimmed)] md:grid">
            <span class="col-span-3 col-start-3">Workflow</span>
            <span class="col-span-4">What happens</span>
            <span class="col-span-3">Trigger</span>
          </div>
          <div
            v-for="flow in workflows"
            :key="flow.name"
            class="group grid gap-3 border-t border-[var(--ui-border)] py-7 md:grid-cols-12 md:gap-8 md:py-8"
          >
            <div class="aspect-[16/10] w-40 overflow-hidden rounded-md border border-[var(--ui-border)] bg-[var(--ui-bg-muted)] transition-transform duration-500 ease-out group-hover:scale-[1.03] md:col-span-2 md:w-auto">
              <img :src="flow.art" alt="" width="512" height="320" loading="lazy" decoding="async" class="h-full w-full object-cover" />
            </div>
            <div class="md:col-span-3">
              <h3 class="text-lg font-medium tracking-[-0.02em] text-[var(--ink)]">{{ flow.title }}</h3>
              <p class="mt-1 font-mono text-[11px] text-[var(--ui-text-dimmed)]">{{ flow.name }}</p>
            </div>
            <p class="max-w-xl text-[15px] leading-relaxed text-[var(--ui-text-toned)] md:col-span-4">{{ flow.body }}</p>
            <div class="flex flex-wrap content-start gap-1.5 md:col-span-3">
              <code
                v-for="trigger in flow.triggers"
                :key="trigger"
                class="rounded-[4px] border border-[var(--ui-border)] bg-[var(--ui-bg-muted)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--ui-text-toned)]"
              >{{ trigger }}</code>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- ─── Lifecycle ─── -->
    <HowItWorks />

    <!-- ─── Review ─── -->
    <section class="scroll-mt-24 border-t border-[var(--ui-border)] py-24 md:py-32" id="code-review">
      <div class="mx-auto max-w-[88rem] px-6 md:px-10">
        <div class="grid gap-12 md:grid-cols-12">
          <div class="md:col-span-5">
            <h2 class="text-[2rem] font-medium leading-[1.02] tracking-[-0.04em] text-[var(--ink)] md:text-[2.75rem]">
              Review that only speaks up for real bugs.
            </h2>
            <p class="mt-6 max-w-sm text-[15px] leading-relaxed text-[var(--ui-text-muted)]">
              A pipeline of agents works every pull request: the diff is analyzed from two angles,
              the findings are merged and deduplicated, and each one is checked against the
              repository before it is posted. What reaches the thread is short and worth reading.
            </p>
          </div>

          <div class="md:col-span-6 md:col-start-7">
            <div class="overflow-hidden rounded-lg border border-[var(--ui-border)] bg-[var(--color-neutral-0)]">
              <div class="flex items-center justify-between border-b border-[var(--ui-border)] px-5 py-3">
                <span class="font-mono text-[11px] text-[var(--ui-text-muted)]">payments/webhooks.py <span class="text-[var(--ui-text-dimmed)]">L118</span></span>
                <span class="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-[var(--ui-text-muted)]">
                  <span class="size-1.5 rounded-full bg-[var(--accent)]"></span>high
                </span>
              </div>
              <div class="px-5 py-5">
                <p class="font-mono text-xs text-[var(--ui-text-dimmed)]"><span class="select-none pr-2">118</span>if user.plan:</p>
                <p class="mt-4 text-[15px] leading-relaxed text-[var(--ui-text-toned)]">
                  <code class="font-mono text-[13px] text-[var(--ink)]">user</code> is nullable on this path. The webhook fires for
                  deleted accounts, so this dereferences before the guard below ever runs.
                </p>
              </div>
              <ul class="border-t border-[var(--ui-border)] px-5 py-4 text-xs text-[var(--ui-text-muted)]">
                <li class="flex justify-between py-1"><span>checked against the repository</span><span class="text-[var(--ink)]">kept</span></li>
                <li v-for="dropped in droppedFindings" :key="dropped" class="flex justify-between py-1 text-[var(--ui-text-dimmed)]">
                  <span class="line-through">{{ dropped }}</span><span>dropped</span>
                </li>
              </ul>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- ─── Self-hosted ─── -->
    <section class="scroll-mt-24 border-t border-[var(--ui-border)] py-24 md:py-32" id="selfhost">
      <div class="mx-auto max-w-[88rem] px-6 md:px-10">
        <div class="grid gap-10 md:grid-cols-12 md:items-center">
          <div class="md:col-span-5">
            <h2 class="text-[2rem] font-medium leading-[1.02] tracking-[-0.04em] text-[var(--ink)] md:text-[2.75rem]">
              Runs where you can see it.
            </h2>
            <p class="mt-5 text-lg font-medium tracking-[-0.01em] text-[var(--ui-text-toned)]">
              Fully self-hosted, free to use.
            </p>
          </div>
          <!-- desktop only: a branch that leaves the line and comes back, drawn live -->
          <div class="pointer-fine-only aspect-[16/10] w-full overflow-hidden rounded-xl bg-[#f7f4ee] ring-1 ring-inset ring-black/10 md:col-span-6 md:col-start-7">
            <ClientOnly>
              <InkCanvas v-if="!touch" src="/ink/review.json" motion="flow" alt="A line of ink strokes with a branch that arcs away and merges back at a red mark" />
              <template #fallback><div class="h-full w-full bg-[#f7f4ee]"></div></template>
            </ClientOnly>
          </div>
        </div>
        <dl class="mt-16 grid gap-x-8 gap-y-10 border-t border-[var(--ui-border)] pt-10 sm:grid-cols-2 md:mt-20 lg:grid-cols-4">
          <div v-for="fact in hostingFacts" :key="fact.title">
            <dt class="text-base font-medium tracking-[-0.02em] text-[var(--ink)]">{{ fact.title }}</dt>
            <dd class="mt-2 text-[15px] leading-relaxed text-[var(--ui-text-muted)]">{{ fact.body }}</dd>
          </div>
        </dl>
      </div>
    </section>

    <!-- ─── Close ─── -->
    <section class="border-t border-[var(--ui-border)] py-28 md:py-40" id="get-started">
      <div class="mx-auto flex max-w-[88rem] flex-col items-start gap-8 px-6 md:flex-row md:items-end md:justify-between md:px-10">
        <h2 class="max-w-xl text-[2.5rem] font-medium leading-[1] tracking-[-0.04em] text-[var(--ink)] md:text-[4rem]">
          Deploy it this afternoon.
        </h2>
        <div class="flex flex-col gap-4">
          <code class="block rounded-md border border-[var(--ui-border)] bg-[var(--ui-bg-muted)] px-4 py-3 font-mono text-[13px] text-[var(--ui-text-toned)]">
            <span class="select-none text-[var(--ui-text-dimmed)]">$ </span>docker compose --profile all up -d
          </code>
          <div class="flex flex-wrap items-center gap-3">
            <UButton size="lg" color="neutral" variant="solid" trailing-icon="i-lucide-arrow-right" to="/docs/deployment/docker-compose">
              Deployment guide
            </UButton>
            <UButton size="lg" color="neutral" variant="ghost" to="/docs/getting-started/quickstart">Try the CLI first</UButton>
          </div>
        </div>
      </div>
    </section>
  </main>
</template>

<script setup lang="ts">
import { useMediaQuery, usePreferredReducedMotion } from '@vueuse/core'
import type { InkMotion } from '~/components/InkCanvas.vue'

// The scroll-driven stage is for a real pointer on a wide screen and nothing else; `.story-pinned`
// and `.story-stacked` gate the two versions on exactly this query, in CSS, so the server and the
// client always agree on the markup and only the styling differs.
const PINNED = '(min-width: 64rem) and (hover: hover) and (pointer: fine)'

// the opening drawing is needed right after hydration, so start fetching it with the document
useHead({
  link: [
    { rel: 'preload', href: '/ink/listens.json', as: 'fetch', crossorigin: 'anonymous', media: PINNED },
    { rel: 'preload', href: '/ink/listens-m.webp', as: 'image', fetchpriority: 'high', media: `not all and ${PINNED}` },
  ],
})

usePageSeo({
  title: 'JeanClode — Autonomous engineering, end to end',
  description: 'Open-source, self-hosted coding agent. It watches Sentry, GitHub and GitLab, decides what deserves a fix, and ships CI-verified pull requests on its own.',
})

useJsonLd({
  '@type': 'SoftwareApplication',
  name: 'JeanClode',
  applicationCategory: 'DeveloperApplication',
  operatingSystem: 'Docker, Kubernetes, Linux, macOS',
  url: 'https://jeanclode.com',
  description: 'Autonomous coding agent that triages Sentry errors and Git issues, writes the fix, verifies it against your own CI, and opens the pull request.',
  license: 'https://www.gnu.org/licenses/agpl-3.0.html',
  isAccessibleForFree: true,
  publisher: { '@id': 'https://jeanclode.com/#organization' },
  offers: { '@type': 'Offer', price: '0', priceCurrency: 'USD' },
})

const beats = [
  {
    title: 'It listens.',
    body: 'Sentry, GitHub, GitLab, a mention in a thread. Every integration you connect is an input, none of them is the story.',
  },
  {
    title: 'It decides.',
    body: 'Triage reads the evidence and the codebase. Real bug, infrastructure noise, or something a human should weigh in on.',
  },
  {
    title: 'It ships.',
    body: 'A fix on its own branch, one pull request per repository touched, and none of them finish until your CI is green.',
  },
  {
    title: 'It remembers.',
    body: 'A correction made once holds across runs. Different day, different agent, same convention.',
  },
]

// One traced ink drawing per beat; the first also serves as the opening and holds until the second wipes in.
const frames: { ink: string; still: string; motion: InkMotion; alt: string }[] = [
  { ink: '/ink/listens.json', still: '/ink/listens-m.webp', motion: 'converge', alt: 'Ink strokes converging from every edge onto seven black bars' },
  { ink: '/ink/decides.json', still: '/ink/decides-m.webp', motion: 'lanes', alt: 'A tangle of ink passing through a gate and leaving sorted into three lanes' },
  { ink: '/ink/ships.json', still: '/ink/ships-m.webp', motion: 'flow', alt: 'A line of ink strokes with one branch arcing away and merging back at a red mark' },
  { ink: '/ink/remembers.json', still: '/ink/remembers-m.webp', motion: 'align', alt: 'Disordered strokes before a red bar, perfectly aligned strokes after it' },
]
const inkFrames = frames.map(f => ({ src: f.ink, motion: f.motion }))
// no hover, no fine pointer: nothing to interact with, so the drawings ship as stills
const touch = useMediaQuery('(hover: none) and (pointer: coarse)')
const pinned = useMediaQuery(PINNED)
const reduced = usePreferredReducedMotion()

// ── The scroll-driven story ──────────────────────────────────────────────────────────────────────
// Everything continuous here — the wipes, the leave, the progress bar, the pointer tilt — is written
// to the DOM as custom properties from a single rAF loop. Only the discrete state below reaches the
// template, so a scroll costs a handful of style writes instead of re-rendering the whole page every
// frame, which is what made the artwork and the beat copy judder on phones.
const stage = ref<HTMLElement | null>(null)
const pin = ref<HTMLElement | null>(null)
const panel = ref<HTMLElement | null>(null)
const tilt = ref<HTMLElement | null>(null)
const copy = ref<HTMLElement | null>(null)
const bar = ref<HTMLElement | null>(null)

const activeBeat = ref(-1)
const opening = ref(true)
const inkActive = ref(true)
// mutated in place: InkCanvas reads it inside its own draw loop, so this never renders the parent
const reveals = reactive(frames.map((_, i) => (i === 0 ? 1 : 0)))

const clamp01 = (t: number) => (t < 0 ? 0 : t > 1 ? 1 : t)
const ease = (t: number) => t * t * (3 - 2 * t)
// enough precision for a sub-pixel wipe, few enough digits that a still scroll writes nothing
const q = (v: number) => Math.round(v * 1000) / 1000

// The section and the pinned panel are both sized in viewport units, which a collapsing mobile URL bar
// leaves alone. window.innerHeight is not: it grows and shrinks with the bar mid-scroll, and measuring
// the travel against it moved the whole story under the reader's finger.
let travel = 0
function measure() {
  const s = stage.value
  const el = pin.value
  if (!s || !el) return
  const top = Number.parseFloat(getComputedStyle(el).top) || 0
  travel = Math.max(s.offsetHeight - el.offsetHeight - top, 0)
}

// Scroll progress → story phase (0 opening, 1–4 one frame per beat, 5 leaving), holding between transitions.
const KEYS: [number, number][] = [[0, 0], [0.05, 0], [0.15, 1], [0.29, 1], [0.37, 2], [0.5, 2], [0.58, 3], [0.71, 3], [0.79, 4], [0.9, 4], [1, 5]]
function phaseFor(p: number) {
  for (let i = 1; i < KEYS.length; i++) {
    const [p0, k0] = KEYS[i - 1]!
    const [p1, k1] = KEYS[i]!
    if (p <= p1) return k0 + ((p - p0) / (p1 - p0)) * (k1 - k0)
  }
  return 5
}

let progress = 0
let phase = 0

// Inside the stage the wheel is stepped: one gesture, one beat, then a lock while the wipe plays.
// Above the first beat and past the last one the browser gets the wheel back.
const REST = [0, 0.22, 0.435, 0.645, 0.845] // progress where each beat sits still
const LOCK_MS = 700
let beatIndex = 0
let lockedUntil = 0
let wheelAcc = 0
const stageTop = () => (stage.value?.getBoundingClientRect().top ?? 0) + window.scrollY
const glideTo = (p: number) => window.scrollTo({ top: stageTop() + p * travel, behavior: 'smooth' })
const onWheel = (e: WheelEvent) => {
  const p = progress
  const down = e.deltaY > 0
  if (travel <= 0 || (p <= 0 && !down) || (p >= 1 && down) || (beatIndex === REST.length - 1 && down && p > 0.83)) return
  e.preventDefault()
  const now = performance.now()
  if (now < lockedUntil) return
  wheelAcc += e.deltaY
  if (Math.abs(wheelAcc) < 40) return
  wheelAcc = 0
  lockedUntil = now + LOCK_MS
  const next = beatIndex + (down ? 1 : -1)
  if (next < 0) return glideTo(-0.02)
  beatIndex = next
  glideTo(REST[next]!)
}

let lastLeave = -1
let lastProgress = -1
const lastReveal = frames.map(() => -1)

function paint() {
  const el = panel.value
  if (el) {
    const leave = q(clamp01(phase - 4))
    if (leave !== lastLeave) {
      lastLeave = leave
      el.style.setProperty('--leave', String(leave))
    }
  }
  // frame i wipes in from the left while the phase crosses i → i + 1, then stays
  for (let i = 1; i < frames.length; i++) {
    const r = q(ease(clamp01(phase - i)))
    if (r === lastReveal[i]) continue
    lastReveal[i] = r
    reveals[i] = r
  }
  const p = q(progress)
  if (bar.value && p !== lastProgress) {
    lastProgress = p
    bar.value.style.setProperty('--progress', String(p))
  }

  const beat = phase < 0.7 || phase >= 4.7 ? -1 : Math.min(Math.round(phase) - 1, beats.length - 1)
  if (beat !== activeBeat.value) activeBeat.value = beat
  if ((phase < 0.25) !== opening.value) opening.value = phase < 0.25
  if ((phase < 4.98) !== inkActive.value) inkActive.value = phase < 4.98
}

// Read and write inside the same frame, so the wipe lands on the pixels the compositor is scrolling to.
// The phase chases the scroll at a capped speed, so a flick of the wheel still plays every wipe.
const CHASE = 1.6
let lastNow = 0
function sample(now: number) {
  const s = stage.value
  if (!s) return
  progress = travel > 0 ? clamp01(-s.getBoundingClientRect().top / travel) : 0
  const target = phaseFor(progress)
  const dt = Math.min(now - lastNow, 50) / 1000
  const diff = target - phase
  phase = Math.abs(diff) < 0.0005 ? target : phase + Math.sign(diff) * Math.min(Math.abs(diff), dt * CHASE)
  lastNow = now
  // keep the wheel step index honest when the reader arrives by scrollbar, keyboard or touch
  if (now > lockedUntil) beatIndex = REST.reduce((best, r, i) => (Math.abs(r - progress) < Math.abs(REST[best]! - progress) ? i : best), 0)
  paint()
}

let raf = 0
let running = false
function loop(now: number) {
  raf = requestAnimationFrame(loop)
  sample(now)
}
function start() {
  if (running) return
  running = true
  lastNow = performance.now()
  raf = requestAnimationFrame(loop)
}
function stop() {
  if (!running) return
  running = false
  cancelAnimationFrame(raf)
  sample(performance.now())
}

// Pointer → gentle 3D tilt, a moving highlight and a slower drift on the copy. The tilt rides its own
// layer inside the panel: the panel itself carries the scroll-driven leave, so a scroll never restarts
// the 0.7s tilt transition. `client` coordinates, not `page`: page coords fold in scrollY, which would
// turn every scroll into a fresh tilt target and leave the panel bobbing.
let tiltRaf = 0
let px = 0
let py = 0
function onMove(e: MouseEvent) {
  const w = window.innerWidth
  const h = window.innerHeight
  if (!w || !h) return
  px = q((e.clientX / w) * 2 - 1)
  py = q((e.clientY / h) * 2 - 1)
  if (tiltRaf) return
  tiltRaf = requestAnimationFrame(() => {
    tiltRaf = 0
    for (const el of [tilt.value, copy.value]) {
      if (!el) continue
      el.style.setProperty('--px', String(px))
      el.style.setProperty('--py', String(py))
    }
  })
}

let ro: ResizeObserver | null = null
let io: IntersectionObserver | null = null

onMounted(() => {
  measure()
  sample(performance.now())
  ro = new ResizeObserver(() => {
    measure()
    sample(performance.now())
  })
  if (stage.value) ro.observe(stage.value)
  if (pin.value) ro.observe(pin.value)
  // the loop only runs while the story is on screen
  io = new IntersectionObserver(entries => (entries[0]?.isIntersecting ? start() : stop()))
  if (stage.value) io.observe(stage.value)
  // listening on the stage only keeps every other scroll on the page passive
  stage.value?.addEventListener('wheel', onWheel, { passive: false })
})

// a tap on a touch device still synthesises a mousemove, so the tilt binds only where there is a pointer
watchEffect((onCleanup) => {
  if (!import.meta.client || touch.value || reduced.value === 'reduce') return
  window.addEventListener('mousemove', onMove, { passive: true })
  onCleanup(() => window.removeEventListener('mousemove', onMove))
})

onUnmounted(() => {
  stop()
  cancelAnimationFrame(tiltRaf)
  ro?.disconnect()
  io?.disconnect()
  stage.value?.removeEventListener('wheel', onWheel)
})

const workflows = [
  {
    name: 'issue_resolve',
    art: '/ink/decides-thumb.webp',
    title: 'Resolve issues',
    body: 'Triage reads the issue and explores the codebase itself, then hands its findings straight to the fixer. Every repository the fix touches gets its own PR, and each must pass its own CI first.',
    triggers: ['jeanclode:resolve'],
  },
  {
    name: 'sentry_fix',
    art: '/ink/listens-thumb.webp',
    title: 'Fix production errors',
    body: 'Errors are collected in windows, triaged in parallel and grouped by root cause. One cause is one PR, even when the fix spans several repositories. Infrastructure noise is classified and skipped.',
    triggers: ['Sentry webhook'],
  },
  {
    name: 'code_review',
    art: '/ink/remembers-thumb.webp',
    title: 'Review pull requests',
    body: 'Two analyzers read the diff in parallel, their findings are merged and deduplicated, and each one is checked against the repository before anything is posted inline. Real issues only.',
    triggers: ['jeanclode:review', 'jeanclode <url>'],
  },
  {
    name: 'jeanclode_respond',
    art: '/ink/ships-thumb.webp',
    title: 'Answer mentions',
    body: 'A planner reads the whole thread and either routes to the right pipeline or handles the ask itself with git, gh and glab: answer a question, edit scope, push a change, or refuse a destructive one.',
    triggers: ['@jeanclode-bot'],
  },
]

const droppedFindings = ['style nitpick', 'duplicate of #1', 'not in the diff']

const hostingFacts = [
  {
    title: 'Agents never hold a token',
    body: 'A credential proxy sidecar injects API keys and Git tokens into outbound HTTP. The Claude subprocess only ever sees placeholders.',
  },
  {
    title: 'One sandbox per run',
    body: 'Every dispatch is its own container or Kubernetes Job with a scoped RBAC role. It reaches the LLM API and your Git host, nothing else.',
  },
  {
    title: 'A pool of credentials',
    body: 'Mix API keys and Claude subscription tokens in priority order. Rate-limited ones are skipped, exhausted work is deferred and redispatched.',
  },
  {
    title: 'AGPL-3.0, on GitHub',
    body: 'Every line, plus ten architecture decision records explaining why it is built the way it is. Fork it, audit it, run it behind your VPN.',
  },
]
</script>

<style scoped>
/* One version or the other, chosen in CSS so the markup is identical on the server and the client.
   Stacked is the default: a pinned stage needs a pointer to be worth the scroll, and on a phone it
   is dragged around by the URL bar for as long as it stays pinned. */
.story-pinned {
  display: none;
}

@media (min-width: 64rem) and (hover: hover) and (pointer: fine) {
  .story-pinned {
    display: block;
  }
  .story-stacked {
    display: none;
  }
}

/* The scroll loop writes --leave and --progress; the pointer writes --px/--py. Everything below is
   derived from them in CSS, so a frame of the story costs a few style writes rather than a re-render. */
.story-panel {
  --leave: 0;
  transform: translate3d(0, calc(var(--leave) * 40px), 0) scale(calc(1 - var(--leave) * 0.06));
  opacity: calc(1 - var(--leave));
  will-change: transform;
}
.story-tilt {
  transform: translate3d(calc(var(--px, 0) * 10px), calc(var(--py, 0) * 6px), 0);
  transition: transform 0.7s cubic-bezier(0.16, 1, 0.3, 1);
  will-change: transform;
}
.story-light {
  background: radial-gradient(30% 30% at 50% 50%, rgba(255, 255, 255, 0.18), transparent 70%);
  transform: translate3d(calc(var(--px, 0) * 25%), calc(var(--py, 0) * 25%), 0);
  will-change: transform;
}
.story-copy {
  transform: translate3d(calc(var(--px, 0) * -6px), calc(var(--py, 0) * -4px), 0);
}
.story-bar {
  transform: scaleX(var(--progress, 0));
}
@media (hover: none) and (pointer: coarse) {
  .pointer-fine-only {
    display: none;
  }
}
</style>
