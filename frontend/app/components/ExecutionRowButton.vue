<script setup lang="ts">
// Row-level action button for a queued/running execution. Queued renders
// exactly like the plain processing AppButton — nothing to cancel yet
// (see executions/route.py: QUEUED has no container, cancelling it would
// race its own dispatch). Running adds a small red pill on the right that
// bounces and pings; on hover it grows to cover the whole button and
// reveal Cancel — desktop only, there's no hover on touch, so mobile keeps
// the plain processing look and cancels from the detail modal instead.
const props = withDefaults(defineProps<{
  status: 'queued' | 'running'
  label: string
  cancelling?: boolean
}>(), {
  cancelling: false,
})

const emit = defineEmits<{
  cancel: []
}>()

const showCancelAffordance = computed(() => props.status === 'running' && !props.cancelling)

function handleCancelClick() {
  if (props.cancelling) return
  emit('cancel')
}
</script>

<template>
  <div class="relative group/cancel min-w-22 flex overflow-hidden rounded-md">
    <AppButton
      :processing="true"
      color="neutral"
      size="xs"
      class="uppercase tracking-wide min-w-22 w-full justify-center transition-opacity duration-150"
      :class="{ 'group-hover/cancel:opacity-0': showCancelAffordance }"
    >
      {{ label }}
    </AppButton>

    <button
      v-if="showCancelAffordance"
      type="button"
      class="execution-row-button-accent absolute right-1.5 top-[20%] h-[60%] w-1.5 cursor-pointer rounded-full bg-error-500 transition-[width,height,right,top,bottom,border-radius] duration-200 ease-out hover:bg-error-600"
      :aria-label="$t('common.cancel')"
      @click.stop="handleCancelClick"
    >
      <span
        aria-hidden="true"
        class="execution-row-button-accent-ping absolute inset-0 rounded-full bg-error-400 group-hover/cancel:hidden"
      />
      <span
        class="absolute inset-0 flex items-center justify-center gap-1 text-xs font-semibold uppercase tracking-wide text-white opacity-0 transition-opacity delay-[90ms] duration-150 group-hover/cancel:opacity-100"
      >
        <UIcon
          name="i-lucide-circle-x"
          class="size-3.5"
        />
        {{ $t('common.cancel') }}
      </span>
    </button>
  </div>
</template>

<style scoped>
.execution-row-button-accent {
  transform-origin: center;
  animation: execution-row-button-pill-bounce 1.6s ease-in-out infinite;
}

.execution-row-button-accent-ping {
  animation: execution-row-button-pill-ping 1.8s cubic-bezier(0, 0, 0.2, 1) infinite;
}

.group\/cancel:hover .execution-row-button-accent {
  right: 0;
  top: 0;
  bottom: 0;
  height: auto;
  width: 100%;
  border-radius: 0.375rem;
  animation: none;
}

@keyframes execution-row-button-pill-bounce {
  0%, 100% { scale: 1 0.85; }
  50% { scale: 1 1; }
}

@keyframes execution-row-button-pill-ping {
  0% { scale: 1; opacity: 0.5; }
  75%, 100% { scale: 1.4; opacity: 0; }
}
</style>
