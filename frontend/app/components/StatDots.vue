<template>
  <div
    ref="grid"
    class="stat-dots pointer-events-none grid"
    :style="{ gridTemplateColumns: `repeat(${COLS}, ${DOT}px)`, gap: `${GAP}px` }"
    aria-hidden="true"
  >
    <span
      v-for="i in COLS * ROWS"
      :key="i"
      class="stat-dot rounded-full"
      :class="[
        i <= filled ? (live ? 'bg-emerald-500 stat-dot-live' : 'bg-neutral-800 dark:bg-neutral-200') : 'bg-neutral-200 dark:bg-neutral-700',
      ]"
      :style="{ 'width': `${DOT}px`, 'height': `${DOT}px`, '--i': i }"
    />
  </div>
</template>

<script setup lang="ts">
const COLS = 12
const ROWS = 5
const DOT = 6
const GAP = 6
const REACH = 36

const props = defineProps<{
  value: number
  live?: boolean
}>()

const grid = ref<HTMLElement>()
const filled = computed(() => Math.min(props.value, COLS * ROWS))
let frame = 0

function ripple(clientX: number, clientY: number) {
  cancelAnimationFrame(frame)
  frame = requestAnimationFrame(() => {
    for (const dot of grid.value?.children ?? []) {
      const el = dot as HTMLElement
      const r = el.getBoundingClientRect()
      const d = Math.hypot(r.left + r.width / 2 - clientX, r.top + r.height / 2 - clientY)
      const k = Math.max(0, 1 - d / REACH)
      el.style.transform = k ? `scale(${1 + k * 0.9})` : ''
      el.style.opacity = k ? String(0.75 + k * 0.25) : ''
    }
  })
}

function reset() {
  cancelAnimationFrame(frame)
  for (const dot of grid.value?.children ?? []) {
    const el = dot as HTMLElement
    el.style.transform = ''
    el.style.opacity = ''
  }
}

onBeforeUnmount(() => cancelAnimationFrame(frame))

defineExpose({ ripple, reset })
</script>

<style scoped>
.stat-dot {
  opacity: 0.75;
  transition: transform 180ms ease-out, opacity 180ms ease-out, background-color 300ms ease;
  animation: dot-in 420ms cubic-bezier(0.22, 1, 0.36, 1) backwards;
  animation-delay: calc(var(--i) * 8ms);
}

.stat-dot-live {
  animation: dot-in 420ms cubic-bezier(0.22, 1, 0.36, 1) backwards, dot-pulse 1.6s ease-in-out infinite;
  animation-delay: calc(var(--i) * 8ms), calc(var(--i) * 120ms);
}

@keyframes dot-in {
  from { transform: scale(0); opacity: 0; }
}

@keyframes dot-pulse {
  50% { opacity: 0.35; }
}

@media (prefers-reduced-motion: reduce) {
  .stat-dot,
  .stat-dot-live {
    animation: none;
    transition: none;
  }
}
</style>
