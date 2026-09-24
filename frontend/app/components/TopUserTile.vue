<template>
  <StatTile
    icon="i-lucide-at-sign"
    :label="$t('dashboard.stats.topUsers')"
    :loading="loading"
  >
    <template #body>
      <ol
        v-if="users.length"
        class="flex flex-col gap-2"
      >
        <li
          v-for="(user, i) in users"
          :key="user.identity_id"
          class="flex flex-col gap-1"
        >
          <div class="flex items-center gap-2 text-sm">
            <UAvatar
              :src="user.avatar_url ?? undefined"
              :alt="user.username ?? '?'"
              size="2xs"
            />
            <span
              class="truncate"
              :class="i === 0 ? 'font-semibold text-neutral-900 dark:text-neutral-50' : 'text-neutral-600 dark:text-neutral-300'"
            >
              {{ user.username ?? '—' }}
            </span>
            <span class="ml-auto shrink-0 text-xs tabular-nums text-neutral-500 dark:text-neutral-400">
              {{ $t('dashboard.stats.mentions', { count: user.pings }, user.pings) }}
            </span>
          </div>
          <div class="h-0.5 overflow-hidden rounded-full bg-neutral-100 dark:bg-neutral-700">
            <div
              class="top-user-bar h-full rounded-full"
              :class="i === 0 ? 'bg-primary' : 'bg-neutral-300 dark:bg-neutral-500'"
              :style="{ width: `${(user.pings / max) * 100}%` }"
            />
          </div>
        </li>
      </ol>
      <p
        v-else
        class="flex min-h-12 items-center text-sm text-neutral-400 dark:text-neutral-500"
      >
        {{ $t('dashboard.stats.noMentions') }}
      </p>
    </template>
  </StatTile>
</template>

<script setup lang="ts">
import type { TopUser } from '~/types/api'

const props = defineProps<{
  users: TopUser[]
  loading?: boolean
}>()

const max = computed(() => Math.max(1, ...props.users.map((u) => u.pings)))
</script>

<style scoped>
.top-user-bar {
  transform-origin: left;
  animation: bar-grow 700ms cubic-bezier(0.22, 1, 0.36, 1) 150ms both;
}

@keyframes bar-grow {
  from { transform: scaleX(0); }
}

@media (prefers-reduced-motion: reduce) {
  .top-user-bar { animation: none; }
}
</style>
