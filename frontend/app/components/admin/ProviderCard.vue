<script setup lang="ts">
/**
 * Read-only summary card for a configured provider.
 *
 * Shared shell for GitHub / GitLab / LLM configured-state views: header
 * with branded icon + status dot + optional "open settings" external link,
 * and a definition-list body built from ``rows``. Masked fields render as
 * ``••••••••`` — pass ``mono: false`` for label-style values.
 */
export interface ProviderCardRow {
  label: string
  value?: string | null
  masked?: boolean
  mono?: boolean
}

defineProps<{
  title: string
  icon: string
  iconBgClass: string
  iconTextClass?: string
  rows: ProviderCardRow[]
  statusLabel: string
  externalUrl?: string | null
  externalLabel?: string
}>()
</script>

<template>
  <div class="rounded-lg border border-neutral-200 dark:border-neutral-700 overflow-hidden">
    <div class="flex items-center gap-3 p-4 bg-neutral-50 dark:bg-neutral-800/40 border-b border-neutral-200 dark:border-neutral-700">
      <div
        class="size-10 rounded-md flex items-center justify-center shrink-0"
        :class="iconBgClass"
      >
        <UIcon
          :name="icon"
          class="size-5"
          :class="iconTextClass ?? 'text-white'"
        />
      </div>
      <div class="flex-1 min-w-0">
        <p class="font-medium text-neutral-900 dark:text-neutral-100 truncate">
          {{ title }}
        </p>
        <p class="text-xs text-neutral-500 flex items-center gap-1.5">
          <span class="size-1.5 rounded-full bg-emerald-500" />
          {{ statusLabel }}
        </p>
      </div>
      <UButton
        v-if="externalUrl"
        :to="externalUrl"
        target="_blank"
        rel="noopener"
        color="neutral"
        variant="outline"
        size="sm"
        trailing-icon="i-lucide-external-link"
      >
        {{ externalLabel }}
      </UButton>
    </div>

    <dl class="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 p-4 text-sm">
      <template
        v-for="row in rows"
        :key="row.label"
      >
        <dt class="text-neutral-500">
          {{ row.label }}
        </dt>
        <dd
          :class="[
            row.mono === false ? '' : 'font-mono',
            row.masked ? 'text-neutral-400' : 'text-neutral-800 dark:text-neutral-200 truncate',
          ]"
        >
          <template v-if="row.masked">
            ••••••••
          </template>
          <template v-else>
            {{ row.value || '—' }}
          </template>
        </dd>
      </template>
    </dl>
  </div>
</template>
