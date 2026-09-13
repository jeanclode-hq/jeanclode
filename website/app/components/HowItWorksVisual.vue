<template>
  <div class="viz" :class="active ? 'is-active' : ''">
    <!-- 01 · Event arrives -->
    <template v-if="step === 0">
      <div class="flex justify-center gap-2" style="--d: 0s">
        <div v-for="(src, i) in sources" :key="src.name" class="chip a-rise" :style="{ '--d': `${0.05 + i * 0.08}s` }">
          <img :src="src.logo" :alt="src.name" class="size-3 opacity-70" />
          <span>{{ src.name }}</span>
        </div>
      </div>

      <div class="a-drop mx-auto mt-7 w-full max-w-[300px]" style="--d: 0.34s">
        <div class="card px-3.5 py-3">
          <div class="mb-2 flex items-center justify-between">
            <span class="mono text-[var(--ui-text-dimmed)]">issue.created</span>
            <span class="dot-alert"></span>
          </div>
          <p class="text-[13px] font-medium text-[var(--ui-text-highlighted)]">TypeError: undefined is not a function</p>
          <p class="mono mt-1 text-[var(--ui-text-dimmed)]">payments/webhooks.py:118 · 42 events</p>
        </div>
      </div>

      <div class="a-line mx-auto mt-3 h-8 w-px bg-gradient-to-b from-[var(--ui-border-accented)] to-transparent" style="--d: 0.62s"></div>

      <div class="a-rise mt-1 flex items-center justify-center gap-2" style="--d: 0.8s">
        <img src="/logo.svg" alt="" class="size-4" />
        <span class="mono text-[var(--ui-text-muted)]">jeanclode</span>
        <span class="pulse-dot"></span>
      </div>
    </template>

    <!-- 02 · Triage -->
    <template v-else-if="step === 1">
      <div class="relative mx-auto w-full max-w-[320px] overflow-hidden rounded-xl">
        <div class="card px-3.5 py-3">
          <p class="text-[13px] font-medium text-[var(--ui-text-highlighted)]">TypeError: undefined is not a function</p>
          <div class="mono mt-2 space-y-1 text-[var(--ui-text-dimmed)]">
            <p>at handleWebhook (webhooks.py:118)</p>
            <p>at dispatch (queue.py:64)</p>
            <p>at worker.run (main.py:22)</p>
          </div>
        </div>
        <div class="scan"></div>
      </div>

      <div class="mt-6 flex flex-wrap justify-center gap-2">
        <div
          v-for="(verdict, i) in verdicts"
          :key="verdict.label"
          class="chip a-rise"
          :class="verdict.picked ? 'chip-picked' : 'opacity-45'"
          :style="{ '--d': `${0.75 + i * 0.12}s` }"
        >
          <UIcon v-if="verdict.picked" name="i-lucide-check" class="size-3" />
          <span>{{ verdict.label }}</span>
        </div>
      </div>
      <p class="mono a-rise mt-4 text-center text-[var(--ui-text-dimmed)]" style="--d: 1.2s">classified in 4.2s</p>
    </template>

    <!-- 03 · Decision -->
    <template v-else-if="step === 2">
      <div class="relative mx-auto w-full max-w-[380px]">
        <svg viewBox="0 0 380 200" class="w-full" fill="none">
          <path
            v-for="(route, i) in routes"
            :key="route.label"
            :d="route.d"
            :class="['route', route.picked ? 'route-picked' : '']"
            :style="{ '--d': `${0.2 + i * 0.12}s` }"
            stroke-width="1.25"
            stroke-linecap="round"
          />
          <circle cx="16" cy="100" r="4" class="node" />
        </svg>

        <div class="pointer-events-none absolute inset-0">
          <div
            v-for="(route, i) in routes"
            :key="route.label"
            class="a-slide absolute right-0 -translate-y-1/2"
            :style="{ top: `${route.y / 2}%`, '--d': `${0.5 + i * 0.12}s` }"
          >
            <span class="chip whitespace-nowrap" :class="route.picked ? 'chip-picked' : 'opacity-40'">{{ route.label }}</span>
          </div>
        </div>
      </div>
    </template>

    <!-- 04 · Fix -->
    <template v-else-if="step === 3">
      <div class="card a-rise mx-auto w-full max-w-[380px] overflow-hidden" style="--d: 0.05s">
        <div class="flex items-center justify-between border-b border-[var(--ui-border)] px-3 py-2">
          <span class="mono text-[var(--ui-text-muted)]">payments/webhooks.py</span>
          <span class="mono text-[var(--ui-text-dimmed)]">+2 −1</span>
        </div>
        <div class="mono space-y-0.5 px-3 py-2.5">
          <p v-for="(line, i) in diff" :key="i" class="a-type" :class="`diff-${line.kind}`" :style="{ '--d': `${0.25 + i * 0.13}s` }">
            <span class="inline-block w-3 select-none opacity-50">{{ line.kind === 'add' ? '+' : line.kind === 'del' ? '−' : '' }}</span>{{ line.text }}
          </p>
        </div>
      </div>

      <div class="a-rise mt-5 flex items-center justify-center gap-2" style="--d: 0.95s">
        <span class="chip"><UIcon name="i-lucide-git-branch" class="size-3 opacity-60" />fix/sentry-4821</span>
      </div>
      <div class="mt-2 flex flex-wrap justify-center gap-2">
        <span v-for="(repo, i) in repos" :key="repo" class="chip a-rise" :style="{ '--d': `${1.1 + i * 0.12}s` }">
          <span class="dot-open"></span>{{ repo }}
        </span>
      </div>
    </template>

    <!-- 05 · Verify -->
    <template v-else-if="step === 4">
      <div class="card mx-auto w-full max-w-[320px] px-3.5 py-3">
        <div class="mb-3 flex items-center justify-between">
          <span class="mono text-[var(--ui-text-muted)]">ci · fix/sentry-4821</span>
          <span class="mono a-rise text-[var(--ui-text-dimmed)]" style="--d: 1.75s">2m 14s</span>
        </div>
        <div class="space-y-2.5">
          <div v-for="(check, i) in checks" :key="check.name" class="flex items-center gap-2.5">
            <span class="tick" :class="check.retried ? 'tick-retry' : ''" :style="{ '--d': `${0.3 + i * 0.28}s` }"></span>
            <span class="mono flex-1 text-[var(--ui-text-muted)]">{{ check.name }}</span>
            <span
              v-if="check.retried"
              class="mono a-flash rounded px-1.5 py-px text-[var(--ui-text-dimmed)] ring-1 ring-[var(--ui-border)]"
              style="--d: 1.15s"
            >retried</span>
          </div>
        </div>
      </div>
      <p class="mono a-rise mt-4 text-center text-[var(--ui-text-dimmed)]" style="--d: 1.9s">all checks passed · agent allowed to finish</p>
    </template>

    <!-- 06 · Review loop -->
    <template v-else>
      <div class="relative mx-auto w-full max-w-[340px]">
        <div class="mb-4 flex justify-center gap-1.5">
          <span
            v-for="n in 7"
            :key="n"
            class="agent-dot"
            :style="{ '--d': `${0.1 + n * 0.07}s` }"
          ></span>
        </div>

        <div class="card px-3.5 py-3">
          <div class="flex items-center justify-between">
            <span class="mono text-[var(--ui-text-muted)]">#482 fix: guard missing plan</span>
            <span class="mono a-rise rounded bg-[var(--ui-bg-muted)] px-1.5 py-px text-[var(--ui-text-dimmed)]" style="--d: 0.9s">round 2</span>
          </div>
          <div class="mt-3 space-y-1.5">
            <p v-for="(item, i) in reviewLog" :key="item" class="mono a-rise text-[var(--ui-text-dimmed)]" :style="{ '--d': `${1 + i * 0.22}s` }">
              {{ item }}
            </p>
          </div>
        </div>

        <div class="a-stamp mt-4 flex items-center justify-center gap-2" style="--d: 1.85s">
          <span class="chip chip-picked"><UIcon name="i-lucide-check" class="size-3" />LGTM</span>
          <span class="chip opacity-70">@your-team notified</span>
        </div>

        <UIcon name="i-lucide-refresh-cw" class="loop-icon absolute -left-1 top-0 size-3.5 text-[var(--ui-text-dimmed)]" />
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
defineProps<{ step: number, active?: boolean }>()

const sources = [
  { name: 'Sentry', logo: '/sentry.svg' },
  { name: 'GitHub', logo: '/github.svg' },
  { name: 'GitLab', logo: '/gitlab.svg' },
]

const verdicts = [
  { label: 'Real bug', picked: true },
  { label: 'Infra noise', picked: false },
  { label: 'Needs context', picked: false },
]

// y is expressed in the 0-200 viewBox space; the wrapper halves it into a percentage
const routes = [
  { label: 'fix pipeline', y: 40, d: 'M20 100 C 120 100, 140 40, 236 40', picked: true },
  { label: 'skip', y: 80, d: 'M20 100 C 120 100, 140 80, 292 80', picked: false },
  { label: 'ask for context', y: 120, d: 'M20 100 C 120 100, 140 120, 210 120', picked: false },
  { label: 'never retried', y: 160, d: 'M20 100 C 120 100, 140 160, 224 160', picked: false },
]

const diff = [
  { kind: 'ctx', text: 'def handle_webhook(user):' },
  { kind: 'del', text: '    if user.plan:' },
  { kind: 'add', text: '    if user and user.plan:' },
  { kind: 'add', text: '        return charge(user.plan)' },
]

const repos = ['api-server #482', 'worker #91']

const checks = [
  { name: 'ruff', retried: false },
  { name: 'mypy', retried: false },
  { name: 'pytest', retried: true },
  { name: 'build', retried: false },
]

const reviewLog = [
  '7 agents · 2 findings addressed',
  'threads resolved · nothing left to fix',
]
</script>

<style scoped>
.viz {
  width: 100%;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  letter-spacing: 0.02em;
}

.card {
  border-radius: 12px;
  border: 1px solid var(--ui-border);
  background: var(--ui-bg);
  box-shadow: 0 18px 40px -28px rgb(0 0 0 / 0.45);
}

.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border-radius: 999px;
  border: 1px solid var(--ui-border);
  background: var(--ui-bg);
  padding: 4px 10px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 10.5px;
  color: var(--ui-text-muted);
}

.chip-picked {
  border-color: var(--ui-text-dimmed);
  color: var(--ui-text-highlighted);
}

.dot-alert,
.pulse-dot,
.dot-open {
  height: 5px;
  width: 5px;
  border-radius: 999px;
  display: inline-block;
}

.dot-alert { background: #f97316; }
.dot-open { background: #22c55e; }

.pulse-dot {
  background: var(--ui-text-dimmed);
  animation: breathe 1.8s ease-in-out infinite;
}

/* Entrance primitives — every element opts in with a --d delay, and they only
   run once the parent step is the active one, so a step replays on re-entry. */
.a-rise,
.a-drop,
.a-slide,
.a-line,
.a-type,
.a-flash,
.a-stamp,
.agent-dot,
.tick,
.route {
  opacity: 0;
}

.is-active .a-rise { animation: rise 0.55s cubic-bezier(0.16, 1, 0.3, 1) var(--d, 0s) both; }
.is-active .a-drop { animation: drop 0.7s cubic-bezier(0.16, 1, 0.3, 1) var(--d, 0s) both; }
.is-active .a-slide { animation: slide 0.55s cubic-bezier(0.16, 1, 0.3, 1) var(--d, 0s) both; }
.is-active .a-line { animation: growline 0.5s ease-out var(--d, 0s) both; }
.is-active .a-type { animation: typein 0.4s ease-out var(--d, 0s) both; }
.is-active .a-flash { animation: flash 1.6s ease-out var(--d, 0s) both; }
.is-active .a-stamp { animation: stamp 0.5s cubic-bezier(0.34, 1.56, 0.64, 1) var(--d, 0s) both; }

@keyframes rise {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: none; }
}

@keyframes drop {
  from { opacity: 0; transform: translateY(-22px) scale(0.97); }
  to { opacity: 1; transform: none; }
}

@keyframes slide {
  from { opacity: 0; transform: translate(12px, -50%); }
  to { opacity: 1; transform: translate(0, -50%); }
}

@keyframes growline {
  from { opacity: 0; transform: scaleY(0); transform-origin: top; }
  to { opacity: 1; transform: scaleY(1); transform-origin: top; }
}

@keyframes typein {
  from { opacity: 0; clip-path: inset(0 100% 0 0); }
  to { opacity: 1; clip-path: inset(0 0 0 0); }
}

@keyframes flash {
  0% { opacity: 0; }
  15% { opacity: 1; }
  70% { opacity: 1; }
  100% { opacity: 0; }
}

@keyframes stamp {
  from { opacity: 0; transform: scale(0.85); }
  to { opacity: 1; transform: scale(1); }
}

@keyframes breathe {
  0%, 100% { opacity: 0.35; transform: scale(0.85); }
  50% { opacity: 1; transform: scale(1); }
}

/* 02 — scan sweep */
.scan {
  position: absolute;
  inset-inline: 0;
  top: 0;
  height: 34px;
  opacity: 0;
  background: linear-gradient(to bottom, transparent, color-mix(in oklab, var(--ui-text-dimmed) 22%, transparent), transparent);
}

.is-active .scan { animation: sweep 1.5s cubic-bezier(0.5, 0, 0.5, 1) 0.15s both; }

@keyframes sweep {
  0% { opacity: 0; transform: translateY(-34px); }
  12% { opacity: 1; }
  85% { opacity: 1; }
  100% { opacity: 0; transform: translateY(120px); }
}

/* 03 — routes draw out of the decision node */
.route {
  stroke: var(--ui-border-accented);
  stroke-dasharray: 300;
  stroke-dashoffset: 300;
}

.route-picked { stroke: var(--ui-text-dimmed); stroke-width: 1.75; }
.node { fill: var(--ui-text-dimmed); }

.is-active .route { animation: draw 0.85s cubic-bezier(0.16, 1, 0.3, 1) var(--d, 0s) both; }

@keyframes draw {
  from { opacity: 0; stroke-dashoffset: 300; }
  to { opacity: 1; stroke-dashoffset: 0; }
}

/* 04 — diff line tinting */
.diff-add { color: #16a34a; }
.diff-del { color: #dc2626; text-decoration: line-through; text-decoration-color: rgb(220 38 38 / 0.35); }
.diff-ctx { color: var(--ui-text-dimmed); }

/* 05 — CI ticks resolve one by one */
.tick {
  position: relative;
  height: 12px;
  width: 12px;
  flex: none;
  border-radius: 999px;
  border: 1px solid var(--ui-border-accented);
}

.tick::after {
  content: '';
  position: absolute;
  inset: 2px;
  border-radius: 999px;
  background: #22c55e;
  opacity: 0;
}

.is-active .tick { animation: rise 0.4s ease-out var(--d, 0s) both; }
.is-active .tick::after { animation: fillin 0.35s ease-out calc(var(--d, 0s) + 0.35s) both; }
.is-active .tick-retry::after { animation: retryfill 1.5s ease-out calc(var(--d, 0s) + 0.15s) both; }

@keyframes fillin {
  from { opacity: 0; transform: scale(0.3); }
  to { opacity: 1; transform: scale(1); }
}

@keyframes retryfill {
  0% { opacity: 0; background: #ef4444; transform: scale(0.3); }
  18% { opacity: 1; background: #ef4444; transform: scale(1); }
  55% { opacity: 1; background: #ef4444; }
  70% { opacity: 0.2; background: #ef4444; }
  85%, 100% { opacity: 1; background: #22c55e; }
}

/* 06 — the seven reviewers, then the loop */
.agent-dot {
  height: 5px;
  width: 5px;
  border-radius: 999px;
  background: var(--ui-border-accented);
}

.is-active .agent-dot { animation: agentpop 0.5s cubic-bezier(0.34, 1.56, 0.64, 1) var(--d, 0s) both; }

@keyframes agentpop {
  from { opacity: 0; transform: scale(0.4); }
  60% { opacity: 1; background: var(--ui-text-dimmed); }
  to { opacity: 1; transform: scale(1); }
}

.loop-icon { opacity: 0; }
.is-active .loop-icon { animation: loopspin 2.4s ease-in-out 0.4s both; }

@keyframes loopspin {
  0% { opacity: 0; transform: rotate(0deg); }
  20% { opacity: 0.9; }
  75% { opacity: 0.9; transform: rotate(360deg); }
  100% { opacity: 0.25; transform: rotate(360deg); }
}

@media (prefers-reduced-motion: reduce) {
  .viz *,
  .viz *::after {
    animation: none !important;
    opacity: 1 !important;
    stroke-dashoffset: 0 !important;
    transform: none !important;
  }
  .scan { display: none; }
}
</style>
