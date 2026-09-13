/**
 * Theme management composable
 *
 * Handles theme switching (light/dark/system) with persistence.
 * Uses Nuxt's useColorMode which handles SSR via cookies to avoid hydration mismatches.
 */
export function useTheme() {
  const colorMode = useColorMode()

  // Use colorMode.preference directly - it's SSR-safe via cookies
  const themePreference = computed<'light' | 'dark' | 'system'>(() => {
    const pref = colorMode.preference
    if (pref === 'light' || pref === 'dark') return pref
    return 'system'
  })

  // Update preference
  function setTheme(value: 'light' | 'dark' | 'system') {
    colorMode.preference = value
  }

  // Theme icon based on current preference
  const currentThemeIcon = computed(() => {
    if (themePreference.value === 'dark') return 'i-lucide-moon'
    if (themePreference.value === 'light') return 'i-lucide-sun'
    return 'i-lucide-monitor'
  })

  // Theme options for the 3-way toggle
  const themeOptions = [
    { value: 'light' as const, icon: 'i-lucide-sun' },
    { value: 'dark' as const, icon: 'i-lucide-moon' },
    { value: 'system' as const, icon: 'i-lucide-monitor' },
  ]

  // Cycle through themes when collapsed (light -> dark only, no system)
  function cycleThemeCollapsed() {
    setTheme(colorMode.value === 'dark' ? 'light' : 'dark')
  }

  return {
    themePreference,
    currentThemeIcon,
    themeOptions,
    setTheme,
    cycleThemeCollapsed,
  }
}
