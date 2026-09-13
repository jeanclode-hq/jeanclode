<template>
  <img
    v-if="src"
    :src="src"
    :class="props.class"
    :alt="alt"
  />
  <UIcon
    v-else-if="fallback"
    :name="fallback"
    :class="props.class"
  />
</template>

<script setup lang="ts">
const props = defineProps<{
  lightSrc?: string
  darkSrc?: string
  fallback?: string
  alt?: string
  class?: string
}>()

const colorMode = useColorMode()
const isDark = computed(() => {
  if (colorMode.value === 'dark') return true
  if (colorMode.value === 'light') return false
  // System preference
  if (import.meta.client) {
    return window.matchMedia('(prefers-color-scheme: dark)').matches
  }
  return false
})
const src = computed(() => {
  if (props.lightSrc && props.darkSrc) {
    return isDark.value ? props.darkSrc : props.lightSrc
  }
  return props.lightSrc || props.darkSrc || null
})
</script>
