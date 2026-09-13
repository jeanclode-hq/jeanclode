<template>
  <UButton
    v-bind="$attrs"
    class="relative overflow-hidden"
    :class="[processing ? 'cursor-wait' : '', disabled ? 'opacity-50 cursor-not-allowed' : '']"
    :disabled="disabled || processing"
    @click="handleClick"
  >
    <!-- Shimmer overlay when processing -->
    <div
      v-if="processing"
      class="absolute inset-0 -translate-x-full animate-shimmer bg-linear-to-r from-transparent via-white/20 to-transparent"
    />

    <!-- Content with crossfade -->
    <Transition
      mode="out-in"
      enter-active-class="transition-all duration-200 ease-out"
      enter-from-class="opacity-0 scale-90"
      enter-to-class="opacity-100 scale-100"
      leave-active-class="transition-all duration-150 ease-in"
      leave-from-class="opacity-100 scale-100"
      leave-to-class="opacity-0 scale-110"
    >
      <span
        v-if="processing"
        key="loading"
        class="flex items-center justify-center w-full"
      >
        <span class="flex gap-1">
          <span
            class="size-1.5 rounded-full bg-current animate-bounce-dot"
            style="animation-delay: 0ms"
          />
          <span
            class="size-1.5 rounded-full bg-current animate-bounce-dot"
            style="animation-delay: 150ms"
          />
          <span
            class="size-1.5 rounded-full bg-current animate-bounce-dot"
            style="animation-delay: 300ms"
          />
        </span>
      </span>
      <span
        v-else
        key="content"
        class="flex items-center justify-center gap-1.5"
      >
        <slot />
        <UIcon
          v-if="icon"
          :name="icon"
          class="size-3 transition-transform duration-150 group-hover:translate-x-0.5"
        />
      </span>
    </Transition>
  </UButton>
</template>

<script setup lang="ts">
defineOptions({
  inheritAttrs: false,
})

withDefaults(defineProps<{
  processing?: boolean
  disabled?: boolean
  icon?: string
}>(), {
  processing: false,
  disabled: false,
  icon: undefined,
})

const emit = defineEmits<{
  click: []
}>()

function handleClick() {
  emit('click')
}
</script>

<style scoped>
@keyframes shimmer {
  100% {
    transform: translateX(200%);
  }
}

@keyframes bounce-dot {
  0%, 100% {
    transform: translateY(0);
    opacity: 0.4;
  }
  50% {
    transform: translateY(-4px);
    opacity: 1;
  }
}

.animate-shimmer {
  animation: shimmer 1.5s infinite;
}

.animate-bounce-dot {
  animation: bounce-dot 600ms ease-in-out infinite;
}
</style>
