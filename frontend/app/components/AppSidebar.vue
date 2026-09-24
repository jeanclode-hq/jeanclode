<template>
  <UDashboardSidebar
    id="app-sidebar"
    resizable
    collapsible
    :min-size="12"
    :default-size="15"
    :max-size="25"
    :collapsed-size="4"
    :ui="{ footer: 'border-t-0 pt-0' }"
  >
    <!-- Header: Logo -->
    <template #header="{ collapsed: isCollapsed, collapse }">
      <div
        class="flex items-center w-full overflow-hidden"
        :class="isCollapsed ? 'justify-center' : 'gap-2'"
      >
        <NuxtLink
          to="/"
          class="flex items-center gap-2 cursor-pointer hover:opacity-80 transition-opacity"
          :class="isCollapsed ? 'justify-center' : ''"
        >
          <LogoMark class="size-6 shrink-0 text-neutral-900 dark:text-neutral-100" />
          <span
            v-if="!isCollapsed"
            class="text-sm font-semibold text-neutral-900 dark:text-neutral-100 truncate whitespace-nowrap"
          >
            {{ $t('common.appName') }}
          </span>
        </NuxtLink>
        <UIcon
          v-if="!isCollapsed"
          name="i-lucide-panel-left-close"
          class="ms-auto size-4 text-neutral-400 dark:text-neutral-500 hover:text-neutral-600 dark:hover:text-neutral-300 cursor-pointer shrink-0"
          @click="collapse?.(true)"
        />
      </div>
    </template>

    <!-- Navigation (Desktop) -->
    <template #default="{ collapsed: isCollapsed, collapse }">
      <UNavigationMenu
        :items="navigationItems"
        orientation="vertical"
        :collapsed="isCollapsed"
        highlight
        :tooltip="isCollapsed"
      />

      <!-- Expand button - only when collapsed -->
      <div class="mt-auto pt-2">
        <button
          v-if="isCollapsed"
          type="button"
          class="cursor-pointer w-full flex justify-center p-2 rounded-lg text-neutral-500 dark:text-neutral-400 hover:bg-neutral-100 dark:hover:bg-neutral-700 hover:text-neutral-700 dark:hover:text-neutral-200 transition-colors"
          @click="collapse?.(false)"
        >
          <UIcon
            name="i-lucide-panel-left-open"
            class="size-[18px]"
          />
        </button>
      </div>
    </template>

    <!-- Footer: Theme + Workspace (Desktop) -->
    <template #footer="{ collapsed: isCollapsed }">
      <div
        class="w-full overflow-hidden"
        :class="isCollapsed ? 'flex flex-col items-center gap-1' : ''"
      >
        <!-- Theme toggle -->
        <ClientOnly>
          <div
            v-if="isCollapsed"
            class="flex justify-center"
          >
            <button
              type="button"
              class="cursor-pointer p-2 rounded-lg w-full text-neutral-500 dark:text-neutral-400 hover:bg-neutral-100 dark:hover:bg-neutral-700 hover:text-neutral-700 dark:hover:text-neutral-200 transition-colors"
              @click="cycleThemeCollapsed"
            >
              <UIcon
                :name="currentThemeIcon"
                class="size-[18px]"
              />
            </button>
          </div>
          <div
            v-else
            class="px-2 pb-2"
          >
            <div class="relative flex w-full rounded-lg bg-neutral-100 dark:bg-neutral-900 p-0.5">
              <div
                class="absolute top-0.5 bottom-0.5 rounded-md bg-white dark:bg-neutral-600 shadow-sm transition-all duration-200 ease-out"
                :style="{
                  width: `calc((100% - 4px) / 3)`,
                  left: `calc(${themeOptions.findIndex(t => t.value === themePreference)} * (100% - 4px) / 3 + 2px)`,
                }"
              />
              <button
                v-for="theme in themeOptions"
                :key="theme.value"
                type="button"
                class="cursor-pointer relative z-10 flex-1 flex justify-center p-1.5 rounded-md transition-colors duration-200"
                :class="themePreference === theme.value ? 'text-neutral-900 dark:text-neutral-100' : 'text-neutral-500 dark:text-neutral-400 hover:text-neutral-700 dark:hover:text-neutral-200'"
                @click="setTheme(theme.value)"
              >
                <UIcon
                  :name="theme.icon"
                  class="size-4"
                />
              </button>
            </div>
          </div>
        </ClientOnly>

        <!-- Workspace Switcher -->
        <div
          v-if="!isCollapsed"
          class="pt-2 border-t border-neutral-200 dark:border-neutral-700"
        >
          <UDropdownMenu
            :items="workspaceSwitcherItems"
            :content="{ side: 'top', align: 'start' }"
          >
            <button
              type="button"
              class="cursor-pointer w-full flex items-center gap-2.5 rounded-lg p-2 text-left hover:bg-neutral-100 dark:hover:bg-neutral-700"
            >
              <UAvatar
                :text="workspaceInitial"
                :ui="{ root: 'bg-gradient-to-br from-neutral-700 to-neutral-900', text: 'text-white' }"
                size="sm"
              />
              <div class="flex items-center gap-2.5 flex-1 min-w-0">
                <span class="flex-1 text-sm font-medium text-neutral-900 dark:text-neutral-100 truncate whitespace-nowrap">
                  {{ workspaceStore.currentWorkspace?.name ?? $t('workspace.select') }}
                </span>
                <UIcon
                  name="i-lucide-chevrons-up-down"
                  class="size-4 text-neutral-400 dark:text-neutral-500 shrink-0"
                />
              </div>
            </button>
          </UDropdownMenu>
        </div>

        <!-- Workspace (collapsed) -->
        <UDropdownMenu
          v-else
          :items="workspaceSwitcherItems"
          :content="{ side: 'top', align: 'center' }"
        >
          <button
            type="button"
            class="cursor-pointer flex justify-center p-2 rounded-lg hover:bg-neutral-100 dark:hover:bg-neutral-700"
          >
            <UAvatar
              :text="workspaceInitial"
              :ui="{ root: 'bg-gradient-to-br from-neutral-700 to-neutral-900', text: 'text-white' }"
              size="xs"
            />
          </button>
        </UDropdownMenu>
      </div>
    </template>

    <!-- Mobile Sidebar Content (Slideover) -->
    <template #content="{ close }">
      <div class="flex flex-col h-full bg-sidebar-bg dark:bg-neutral-950">
        <!-- Mobile Header -->
        <div class="h-14 shrink-0 flex items-center gap-2 px-4 border-b border-neutral-200 dark:border-neutral-700">
          <NuxtLink
            to="/"
            class="flex items-center gap-2 cursor-pointer hover:opacity-80 transition-opacity"
            @click="close?.()"
          >
            <LogoMark class="size-6 shrink-0 text-neutral-900 dark:text-neutral-100" />
            <span class="text-sm font-semibold text-neutral-900 dark:text-neutral-100">
              {{ $t('common.appName') }}
            </span>
          </NuxtLink>
          <UIcon
            name="i-lucide-x"
            class="ms-auto size-5 text-neutral-400 dark:text-neutral-500 hover:text-neutral-600 dark:hover:text-neutral-300 cursor-pointer"
            @click="close?.()"
          />
        </div>

        <!-- Mobile Workspace Switcher -->
        <div class="px-2 pt-3 pb-2 mb-1 border-b border-neutral-200 dark:border-neutral-700">
          <UDropdownMenu
            :items="workspaceSwitcherItems"
            :content="{ align: 'start' }"
          >
            <button
              type="button"
              class="cursor-pointer w-full flex items-center gap-2 rounded-lg p-2 text-left hover:bg-neutral-100 dark:hover:bg-neutral-700 transition-colors"
            >
              <UAvatar
                :text="workspaceInitial"
                :ui="{ root: 'bg-gradient-to-br from-neutral-700 to-neutral-900' }"
                size="xs"
              />
              <span class="flex-1 text-sm font-medium text-neutral-900 dark:text-neutral-100 truncate">
                {{ workspaceStore.currentWorkspace?.name ?? $t('workspace.select') }}
              </span>
              <UIcon
                name="i-lucide-chevrons-up-down"
                class="size-4 text-neutral-400 dark:text-neutral-500 shrink-0"
              />
            </button>
          </UDropdownMenu>
        </div>

        <!-- Mobile Navigation -->
        <div class="flex-1 overflow-y-auto px-2 py-3">
          <UNavigationMenu
            :items="navigationItems"
            orientation="vertical"
            highlight
            @click="close?.()"
          />

          <!-- Mobile: Theme -->
          <div class="mt-4 pt-4 border-t border-neutral-200 dark:border-neutral-700 flex flex-col gap-1">
            <ClientOnly>
              <div class="px-2.5 py-2">
                <div class="flex w-full rounded-lg bg-neutral-100 dark:bg-neutral-900 p-0.5">
                  <button
                    v-for="theme in themeOptions"
                    :key="theme.value"
                    type="button"
                    class="cursor-pointer flex-1 flex justify-center p-1.5 rounded-md transition-colors"
                    :class="themePreference === theme.value ? 'bg-white dark:bg-neutral-600 shadow-sm text-neutral-900 dark:text-neutral-100' : 'text-neutral-500 dark:text-neutral-400 hover:text-neutral-700 dark:hover:text-neutral-200'"
                    @click="setTheme(theme.value)"
                  >
                    <UIcon
                      :name="theme.icon"
                      class="size-4"
                    />
                  </button>
                </div>
              </div>
            </ClientOnly>
          </div>
        </div>
      </div>
    </template>
  </UDashboardSidebar>

  <!-- Workspace Creation Modal (triggered from switcher) -->
  <WorkspaceCreateModal v-model:open="showCreateModal" />
</template>

<script setup lang="ts">
import type { NavigationMenuItem } from '@nuxt/ui'

interface NavItem {
  label: string
  icon?: string
  to?: string
  badge?: string
  active?: boolean
  onSelect?: () => void
}

const props = defineProps<{
  items: NavItem[][]
}>()

const { t } = useI18n()

// Workspace switcher
const workspaceStore = useWorkspaceStore()
const showCreateModal = ref(false)

const workspaceInitial = computed(() => {
  const name = workspaceStore.currentWorkspace?.name
  return name ? name.charAt(0).toUpperCase() : 'W'
})

const workspaceSwitcherItems = computed(() => {
  const wsItems = workspaceStore.workspaces.map((ws) => ({
    label: ws.name,
    icon: ws.id === workspaceStore.currentWorkspace?.id ? 'i-lucide-check' : 'i-lucide-building-2',
    onSelect: () => workspaceStore.switchWorkspace(ws),
  }))

  const groups: Record<string, unknown>[][] = []
  if (wsItems.length > 0) {
    groups.push(wsItems)
  }
  groups.push([
    {
      label: t('workspace.create.trigger'),
      icon: 'i-lucide-plus',
      onSelect: () => {
        showCreateModal.value = true
      },
    },
  ])
  return groups
})

// Theme management
const {
  themePreference,
  currentThemeIcon,
  themeOptions,
  setTheme,
  cycleThemeCollapsed,
} = useTheme()

// Transform nav items to NavigationMenu format
const navigationItems = computed<NavigationMenuItem[][]>(() =>
  props.items.map((group) =>
    group.map((item) => ({
      label: item.label,
      icon: item.icon,
      to: item.to,
      badge: item.badge,
      active: item.active,
      onSelect: item.onSelect,
    })),
  ),
)
</script>
