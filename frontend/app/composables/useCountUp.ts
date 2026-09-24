export function useCountUp(target: () => number | undefined, duration = 700) {
  const display = ref(0)
  let frame = 0

  function animate(from: number, to: number) {
    cancelAnimationFrame(frame)
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (reduced || from === to) {
      display.value = to
      return
    }
    const start = performance.now()
    const step = (now: number) => {
      const t = Math.min((now - start) / duration, 1)
      const eased = 1 - (1 - t) ** 3
      display.value = Math.round(from + (to - from) * eased)
      if (t < 1) frame = requestAnimationFrame(step)
    }
    frame = requestAnimationFrame(step)
  }

  onMounted(() => {
    watch(target, (to) => {
      if (to !== undefined) animate(display.value, to)
    }, { immediate: true })
  })
  onBeforeUnmount(() => cancelAnimationFrame(frame))

  return display
}
