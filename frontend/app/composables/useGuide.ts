import type { InjectionKey } from 'vue'

export function useGuide() {
  const isOpen = useState('guide.open', () => false)
  const index = useState('guide.index', () => 0)

  const current = computed(() => GUIDE_STEPS[index.value]!)
  const isFirst = computed(() => index.value === 0)
  const isLast = computed(() => index.value === GUIDE_STEPS.length - 1)

  function open(chapterId?: string) {
    const start = chapterId ? GUIDE_STEPS.findIndex((s) => s.chapter.id === chapterId) : 0
    index.value = Math.max(start, 0)
    isOpen.value = true
  }

  function goTo(i: number) {
    index.value = Math.min(Math.max(i, 0), GUIDE_STEPS.length - 1)
  }

  return {
    isOpen,
    index: readonly(index),
    current,
    isFirst,
    isLast,
    open,
    close: () => {
      isOpen.value = false
    },
    next: () => goTo(index.value + 1),
    back: () => goTo(index.value - 1),
    goTo,
  }
}

export const GUIDE_DEMO: InjectionKey<boolean> = Symbol('guide-demo')
export const GUIDE_SETTINGS_TAB: InjectionKey<MaybeRefOrGetter<string>> = Symbol('guide-settings-tab')

// Pages rendered inside the guide read the demo workspace, which guideDemo.ts
// answers locally; everywhere else this is just the current workspace.
export function useActiveWorkspaceId() {
  const workspaceStore = useWorkspaceStore()
  const demo = inject(GUIDE_DEMO, false)
  return computed(() => (demo ? DEMO_WORKSPACE_ID : workspaceStore.currentWorkspace?.id))
}
