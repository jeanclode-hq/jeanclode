<script setup lang="ts">
import type { ExecutionWorkflow } from '~/types/api'

const props = defineProps<{
  // Badge is a dumb display component: it just looks up a color/label for
  // whatever status string it's given, whether that's a raw Execution status
  // (PR/review rows) or an issue's computed display status (pr_open, etc.).
  status: string
  workflow?: ExecutionWorkflow
}>()

const label = computed(() =>
  getExecStatusLabel(props.status, props.workflow ?? 'fix'),
)

const color = computed(() => getExecStatusColor(props.status))
</script>

<template>
  <UBadge
    :color="(color as any)"
    variant="subtle"
    size="xs"
    class="inline-block w-24 text-center"
  >
    {{ label }}
  </UBadge>
</template>
