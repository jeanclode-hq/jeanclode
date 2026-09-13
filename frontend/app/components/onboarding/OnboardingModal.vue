<script setup lang="ts">
const workspaceStore = useWorkspaceStore()
const onboarding = useOnboarding()

const steps = [
  { title: 'Git Provider', description: 'Connect your code' },
  { title: 'Connectors', description: 'Add integrations' },
]

// SSE connection for real-time updates during onboarding
let sseConn: ReturnType<typeof useEventStream> | null = null

watch(
  () => onboarding.showModal.value && workspaceStore.currentWorkspace?.id,
  (shouldConnect) => {
    if (shouldConnect && workspaceStore.currentWorkspace) {
      sseConn = useEventStream(workspaceStore.currentWorkspace.id)
      sseConn.connect()
    } else if (sseConn) {
      sseConn.disconnect()
      sseConn = null
    }
  },
  { immediate: true },
)

onUnmounted(() => {
  sseConn?.disconnect()
})

const workspaceId = computed(() => workspaceStore.currentWorkspace?.id ?? '')

const STEP_ORDER: ('git_provider' | 'connectors')[] = ['git_provider', 'connectors']

const slideDirection = ref<'forward' | 'back'>('forward')

function goToStep(index: number) {
  if (index < onboarding.stepperIndex.value) {
    slideDirection.value = 'back'
    onboarding.setStep(STEP_ORDER[index])
  }
}
</script>

<template>
  <UModal
    v-model:open="onboarding.showModal.value"
    :title="$t('onboarding.title')"
    :close="{ onClick: () => onboarding.showModal.value = false }"
    :ui="{
      content: 'max-w-full sm:max-w-2xl h-full sm:h-auto',
      title: 'sr-only',
    }"
  >
    <template #body>
      <div class="px-2 py-4">
        <!-- Header -->
        <div class="text-center mb-8">
          <div class="inline-flex items-center justify-center size-12 rounded-2xl bg-neutral-100 dark:bg-neutral-800 mb-4">
            <LogoMark class="size-6 text-neutral-700 dark:text-neutral-200" />
          </div>
          <h2 class="text-xl font-semibold text-neutral-900 dark:text-neutral-100">
            {{ $t('onboarding.title') }}
          </h2>
          <p class="text-sm text-neutral-500 dark:text-neutral-400 mt-1">
            {{ $t('onboarding.description') }}
          </p>
        </div>

        <!-- Stepper -->
        <div class="flex items-center mb-8 px-12">
          <template
            v-for="(s, index) in steps"
            :key="index"
          >
            <button
              type="button"
              class="flex flex-col items-center gap-1.5 relative"
              :class="index < onboarding.stepperIndex.value ? 'cursor-pointer group' : 'cursor-default'"
              :disabled="index >= onboarding.stepperIndex.value"
              @click="goToStep(index)"
            >
              <div
                class="flex items-center justify-center size-8 rounded-full text-xs font-semibold transition-all"
                :class="[
                  index === onboarding.stepperIndex.value
                    ? 'bg-neutral-900 text-white ring-4 ring-neutral-200 dark:ring-neutral-700'
                    : index < onboarding.stepperIndex.value
                      ? 'bg-green-500 text-white group-hover:ring-4 group-hover:ring-green-100 dark:group-hover:ring-green-900'
                      : 'bg-neutral-100 dark:bg-neutral-800 text-neutral-400 dark:text-neutral-500 ring-1 ring-neutral-200 dark:ring-neutral-700',
                ]"
              >
                <UIcon
                  v-if="index < onboarding.stepperIndex.value"
                  name="i-lucide-check"
                  class="size-4"
                />
                <span v-else>{{ index + 1 }}</span>
              </div>
              <span
                class="text-[11px] font-medium whitespace-nowrap"
                :class="[
                  index === onboarding.stepperIndex.value
                    ? 'text-neutral-900 dark:text-neutral-100'
                    : index < onboarding.stepperIndex.value
                      ? 'text-green-600 dark:text-green-400'
                      : 'text-neutral-400 dark:text-neutral-500',
                ]"
              >
                {{ s.title }}
              </span>
            </button>

            <!-- Connector line -->
            <div
              v-if="index < steps.length - 1"
              class="flex-1 h-px mx-4 mt-[-18px]"
              :class="[
                index < onboarding.stepperIndex.value
                  ? 'bg-green-400'
                  : 'bg-neutral-200 dark:bg-neutral-700',
              ]"
            />
          </template>
        </div>

        <!-- Step content -->
        <div class="min-h-[340px] relative overflow-hidden">
          <Transition
            enter-active-class="transition-all duration-300 ease-out"
            leave-active-class="transition-all duration-200 ease-in absolute inset-0"
            :enter-from-class="slideDirection === 'forward' ? 'translate-x-8 opacity-0' : '-translate-x-8 opacity-0'"
            enter-to-class="translate-x-0 opacity-100"
            leave-from-class="translate-x-0 opacity-100"
            :leave-to-class="slideDirection === 'forward' ? '-translate-x-8 opacity-0' : 'translate-x-8 opacity-0'"
            mode="out-in"
          >
            <StepGitProvider
              v-if="onboarding.stepperIndex.value === 0"
              :key="0"
              :workspace-id="workspaceId"
              @continue="slideDirection = 'forward'; onboarding.setStep('connectors')"
            />
            <StepConnectors
              v-else-if="onboarding.stepperIndex.value === 1"
              :key="1"
              :workspace-id="workspaceId"
              :git-org-ids="onboarding.gitOrgIds.value"
              @complete="onboarding.complete()"
              @back="slideDirection = 'back'; onboarding.setStep('git_provider')"
            />
          </Transition>
        </div>
      </div>
    </template>
  </UModal>
</template>
