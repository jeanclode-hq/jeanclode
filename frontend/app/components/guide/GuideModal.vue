<script setup lang="ts">
const { t } = useI18n()
const guide = useGuide()

const chapter = computed(() => guide.current.value.chapter)
const step = computed(() => guide.current.value.step)
const chapterIndex = computed(() => GUIDE_CHAPTERS.indexOf(chapter.value))

// Copy marks code with backticks; vue-i18n has no inline markup of its own.
const bodyParts = computed(() =>
  t(`guide.steps.${step.value.id}.body`)
    .split('`')
    .map((text, i) => ({ text, code: i % 2 === 1 })),
)

function onKey(e: KeyboardEvent) {
  if (!guide.isOpen.value) return
  if (e.key === 'ArrowRight') guide.next()
  if (e.key === 'ArrowLeft') guide.back()
}
onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <UModal
    v-model:open="guide.isOpen.value"
    :title="$t('guide.title')"
    :ui="{
      content: 'max-w-full sm:max-w-[min(1360px,94vw)] h-full sm:h-[min(820px,90vh)] overflow-hidden',
      header: 'hidden',
      body: 'p-0 sm:p-0 h-full',
    }"
  >
    <template #body>
      <div class="grid h-full md:grid-cols-[300px_minmax(0,1fr)]">
        <aside class="flex flex-col min-h-0 border-r border-neutral-200 dark:border-neutral-700 p-5">
          <div class="flex items-center justify-between mb-3">
            <p class="text-xs text-neutral-400 dark:text-neutral-500">
              {{ $t('guide.progress', { current: guide.index.value + 1, total: GUIDE_STEPS.length }) }}
            </p>
            <UButton
              icon="i-lucide-x"
              color="neutral"
              variant="ghost"
              size="xs"
              :aria-label="$t('guide.close')"
              @click="guide.close()"
            />
          </div>

          <nav class="space-y-0.5 mb-5">
            <button
              v-for="(c, i) in GUIDE_CHAPTERS"
              :key="c.id"
              type="button"
              class="cursor-pointer w-full flex items-center gap-2 rounded-md px-2 py-1.5 text-sm text-left transition-colors outline-none focus-visible:ring-2 focus-visible:ring-neutral-300 dark:focus-visible:ring-neutral-600"
              :class="i === chapterIndex
                ? 'bg-neutral-100 dark:bg-neutral-800 text-neutral-900 dark:text-neutral-100 font-medium'
                : 'text-neutral-500 dark:text-neutral-400 hover:bg-neutral-50 dark:hover:bg-neutral-800/60'"
              @click="guide.open(c.id)"
            >
              <UIcon
                :name="i < chapterIndex ? 'i-lucide-check' : c.icon"
                class="size-4 shrink-0"
                :class="i < chapterIndex ? 'text-green-500' : ''"
              />
              {{ $t(`guide.chapters.${c.id}`) }}
            </button>
          </nav>

          <div class="flex-1 min-h-0 overflow-y-auto border-t border-neutral-200 dark:border-neutral-700 pt-4">
            <h3 class="text-base font-semibold text-neutral-900 dark:text-neutral-100 mb-2">
              {{ $t(`guide.steps.${step.id}.title`) }}
            </h3>
            <p class="text-sm leading-relaxed text-neutral-600 dark:text-neutral-300">
              <template
                v-for="(part, i) in bodyParts"
                :key="i"
              >
                <code
                  v-if="part.code"
                  class="text-[12px] px-1 py-0.5 rounded bg-neutral-100 dark:bg-neutral-800 text-neutral-800 dark:text-neutral-200"
                >{{ part.text }}</code>
                <template v-else>
                  {{ part.text }}
                </template>
              </template>
            </p>
          </div>

          <div class="flex items-center gap-2 pt-4">
            <UButton
              v-if="!guide.isFirst.value"
              :label="$t('guide.back')"
              color="neutral"
              variant="outline"
              size="sm"
              icon="i-lucide-arrow-left"
              @click="guide.back()"
            />
            <UButton
              v-if="guide.isLast.value"
              :label="$t('guide.finish')"
              size="sm"
              class="ms-auto"
              @click="guide.close()"
            />
            <UButton
              v-else
              :label="$t('guide.next')"
              size="sm"
              class="ms-auto"
              trailing-icon="i-lucide-arrow-right"
              @click="guide.next()"
            />
          </div>
        </aside>

        <GuideScene
          class="hidden md:block"
          :view="chapter.id"
          :target="step.id"
          :limit="step.limit"
          :settings-tab="step.settingsTab"
          :hint="$t(`guide.steps.${step.id}.hint`)"
        />
      </div>
    </template>
  </UModal>
</template>
