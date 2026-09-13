<script setup lang="ts">
/** Renders text that truncates with an ellipsis, and shows a tooltip with the
 *  full value only when it actually overflows. Width is controlled by the
 *  caller via `class` (e.g. `max-w-48`). */
const props = defineProps<{
  text: string
  class?: string
}>()

const el = ref<HTMLElement | null>(null)
const isTruncated = ref(false)

function measure() {
  const node = el.value
  if (node) isTruncated.value = node.scrollWidth > node.clientWidth + 1
}

let observer: ResizeObserver | null = null

onMounted(() => {
  measure()
  observer = new ResizeObserver(measure)
  if (el.value) observer.observe(el.value)
})

onBeforeUnmount(() => observer?.disconnect())

watch(() => props.text, () => nextTick(measure))
</script>

<template>
  <UTooltip
    :text="text"
    :disabled="!isTruncated"
    :delay-duration="200"
  >
    <span
      ref="el"
      class="block truncate"
      :class="props.class"
    >{{ text }}</span>
  </UTooltip>
</template>
