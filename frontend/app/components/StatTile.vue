<template>
  <div
    class="stat-tile @container relative min-w-0 overflow-hidden bg-white dark:bg-neutral-800"
    @pointermove="onMove"
    @pointerleave="onLeave"
  >
    <StatDots
      v-if="hasDots"
      ref="dots"
      :value="value!"
      :live="live"
      class="absolute right-5 top-1/2 hidden -translate-y-1/2 @min-[20rem]:grid"
    />
    <div
      class="flex min-w-0 flex-col gap-2 px-5 py-4"
      :class="hasDots && '@min-[20rem]:pr-44'"
    >
      <div class="flex items-center gap-1.5 text-xs font-medium text-neutral-500 dark:text-neutral-400">
        <UIcon
          :name="icon"
          class="size-3.5 shrink-0"
        />
        <span class="truncate">{{ label }}</span>
        <span
          v-if="live"
          class="relative flex size-2 shrink-0"
          :aria-label="label"
        >
          <span class="absolute inline-flex size-full animate-ping rounded-full bg-emerald-400 opacity-60" />
          <span class="relative inline-flex size-2 rounded-full bg-emerald-500" />
        </span>
      </div>

      <div
        v-if="loading"
        class="h-8 w-16 rounded-md bg-neutral-100 dark:bg-neutral-700 animate-pulse"
      />
      <div
        v-else-if="$slots.body"
        class="stat-enter"
      >
        <slot name="body" />
      </div>
      <div
        v-else
        class="stat-enter relative flex min-h-8 items-center gap-2 text-3xl font-semibold leading-none tracking-tight text-neutral-900 dark:text-neutral-50"
      >
        {{ formatted }}
      </div>

      <div
        v-if="loading"
        class="h-3 w-24 rounded bg-neutral-100 dark:bg-neutral-700 animate-pulse"
      />
      <p
        v-else-if="!$slots.body"
        class="stat-enter min-h-4 truncate text-xs text-neutral-500 dark:text-neutral-400"
      >
        {{ hint }}
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
const props = defineProps<{
  icon: string
  label: string
  value?: number
  hint?: string
  live?: boolean
  loading?: boolean
}>()

const compact = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 })

const slots = useSlots()
const hasDots = computed(() => !props.loading && props.value !== undefined && !slots.body)

const counted = useCountUp(() => props.value)

const formatted = computed(() => (props.value === undefined ? '—' : compact.format(counted.value)))

const dots = ref<{ ripple: (x: number, y: number) => void, reset: () => void }>()

function onMove(event: PointerEvent) {
  const el = event.currentTarget as HTMLElement
  const rect = el.getBoundingClientRect()
  el.style.setProperty('--spot-x', `${event.clientX - rect.left}px`)
  el.style.setProperty('--spot-y', `${event.clientY - rect.top}px`)
  dots.value?.ripple(event.clientX, event.clientY)
}

function onLeave() {
  dots.value?.reset()
}
</script>

<style scoped>
.stat-tile::before {
  content: '';
  position: absolute;
  inset: 0;
  pointer-events: none;
  opacity: 0;
  transition: opacity 250ms ease;
  background: radial-gradient(220px circle at var(--spot-x, 50%) var(--spot-y, 50%), rgb(0 0 0 / 0.035), transparent 70%);
}

:global(.dark) .stat-tile::before {
  background: radial-gradient(220px circle at var(--spot-x, 50%) var(--spot-y, 50%), rgb(255 255 255 / 0.05), transparent 70%);
}

.stat-tile:hover::before {
  opacity: 1;
}
</style>
