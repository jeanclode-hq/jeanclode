<script setup lang="ts">
import type { GuideView } from '~/utils/guide'
import HomePage from '~/pages/index.vue'
import IssuesPage from '~/pages/issues.vue'
import PullRequestsPage from '~/pages/pull-requests.vue'
import SettingsPage from '~/pages/settings.vue'

const props = defineProps<{
  view: GuideView
  target: string
  limit?: number
  hint: string
  settingsTab?: string
}>()

// The pages lay out for the dashboard's main column, so they render at that
// width and get scaled down to fit the modal.
const PAGE_WIDTH = 1120
const SPOTLIGHT_PAD = 6

provide(GUIDE_DEMO, true)
const { user } = useAuth()
setGuideDemoViewer(user.value)
provide(GUIDE_SETTINGS_TAB, () => props.settingsTab ?? 'account')

const scroller = ref<HTMLElement | null>(null)
const stage = ref<HTMLElement | null>(null)
const page = ref<HTMLElement | null>(null)

const scale = ref(1)
const pageHeight = ref(0)
const viewportHeight = ref(0)
const spot = ref<{ top: number, left: number, width: number, height: number } | null>(null)

function measure() {
  if (!scroller.value || !page.value) return
  scale.value = Math.min(scroller.value.clientWidth / PAGE_WIDTH, 1)
  pageHeight.value = page.value.offsetHeight
  viewportHeight.value = scroller.value.clientHeight
}

function locate(): typeof spot.value {
  if (!stage.value) return null
  const origin = stage.value.getBoundingClientRect()
  const rects = [...stage.value.querySelectorAll<HTMLElement>(`[data-guide="${props.target}"]`)]
    .map((el) => el.getBoundingClientRect())
    .filter((r) => r.width > 0 && r.height > 0)
    .slice(0, props.limit)
  if (!rects.length) return null
  const top = Math.min(...rects.map((r) => r.top)) - origin.top
  const left = Math.min(...rects.map((r) => r.left)) - origin.left
  const bottom = Math.max(...rects.map((r) => r.bottom)) - origin.top
  const right = Math.max(...rects.map((r) => r.right)) - origin.left
  return {
    top: top - SPOTLIGHT_PAD,
    left: left - SPOTLIGHT_PAD,
    width: right - left + SPOTLIGHT_PAD * 2,
    height: bottom - top + SPOTLIGHT_PAD * 2,
  }
}

let scrolledFor = ''
function update() {
  measure()
  spot.value = locate()
  // Scroll once per step, as soon as its anchor exists — the page fills in
  // after its queries resolve, so that isn't necessarily on the first pass.
  if (spot.value && scrolledFor !== props.target && scroller.value) {
    scrolledFor = props.target
    scroller.value.scrollTo({ top: Math.max(spot.value.top - 80, 0), behavior: 'smooth' })
  }
}

let frame = 0
function scheduleUpdate() {
  cancelAnimationFrame(frame)
  frame = requestAnimationFrame(update)
}

let resizeObserver: ResizeObserver | null = null
let mutationObserver: MutationObserver | null = null

onMounted(() => {
  resizeObserver = new ResizeObserver(scheduleUpdate)
  if (scroller.value) resizeObserver.observe(scroller.value)
  if (page.value) resizeObserver.observe(page.value)
  mutationObserver = new MutationObserver(scheduleUpdate)
  if (page.value) mutationObserver.observe(page.value, { childList: true, subtree: true })
  scheduleUpdate()
})

onBeforeUnmount(() => {
  cancelAnimationFrame(frame)
  resizeObserver?.disconnect()
  mutationObserver?.disconnect()
})

watch(() => [props.target, props.view], () => {
  scrolledFor = ''
  scheduleUpdate()
})

// Below the spotlight unless that runs off the bottom of the page — a short
// page leaves the whole viewport free, so it still counts as room.
// These two pages size their table to the window (100vh) and scroll inside it,
// so they get the scene's visible height instead of growing past it.
const fitsViewport = computed(() => props.view === 'pullRequests' || props.view === 'issues')

const cardBelow = computed(() =>
  !spot.value
  || spot.value.top + spot.value.height + 110 < Math.max(pageHeight.value * scale.value, viewportHeight.value),
)
</script>

<template>
  <div
    ref="scroller"
    class="relative h-full overflow-y-auto overflow-x-hidden bg-neutral-50 dark:bg-neutral-900"
  >
    <div
      ref="stage"
      class="relative"
      :style="{ height: `${pageHeight * scale}px` }"
    >
      <div
        ref="page"
        inert
        class="absolute top-0 left-0 p-6 origin-top-left select-none"
        :class="{ 'guide-fit': fitsViewport }"
        :style="{
          width: `${PAGE_WIDTH}px`,
          height: fitsViewport ? `${viewportHeight / scale}px` : undefined,
          transform: `scale(${scale})`,
        }"
      >
        <HomePage v-if="view === 'home'" />
        <PullRequestsPage v-else-if="view === 'pullRequests'" />
        <IssuesPage v-else-if="view === 'issues'" />
        <GitOrgDetail
          v-else-if="view === 'gitOrg'"
          :org="DEMO_GIT_ORG"
          :workspace-id="DEMO_WORKSPACE_ID"
        />
        <SentryOrgDetail
          v-else-if="view === 'sentry'"
          :org="DEMO_SENTRY_ORG"
          :workspace-id="DEMO_WORKSPACE_ID"
        />
        <SettingsPage
          v-else
          :key="settingsTab"
        />
      </div>

      <div
        v-if="spot"
        class="absolute rounded-xl ring-2 ring-primary-500 pointer-events-none transition-all duration-300 ease-out shadow-[0_0_0_9999px_rgba(10,10,10,0.45)]"
        :style="{ top: `${spot.top}px`, left: `${spot.left}px`, width: `${spot.width}px`, height: `${spot.height}px` }"
      />

      <Transition
        enter-active-class="transition-all duration-200 ease-out"
        enter-from-class="opacity-0 translate-y-1"
        mode="out-in"
      >
        <div
          v-if="spot && hint"
          :key="target"
          class="absolute right-4 w-64 rounded-xl border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 shadow-lg px-3.5 py-3"
          :style="cardBelow
            ? { top: `${spot.top + spot.height + 10}px` }
            : { top: `${spot.top - 10}px`, transform: 'translateY(-100%)' }"
        >
          <p class="text-xs leading-relaxed text-neutral-700 dark:text-neutral-200">
            {{ hint }}
          </p>
        </div>
      </Transition>
    </div>
  </div>
</template>

<style scoped>
.guide-fit > :deep(*) {
  height: 100%;
}
</style>
