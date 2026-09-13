<script setup lang="ts">
const props = defineProps<{
  orgId: string
}>()

const { t } = useI18n()
const toast = useToast()

const orgIdRef = computed(() => props.orgId)
const { data: servers, isLoading } = useMcpServersQuery(orgIdRef)
const createMutation = useCreateMcpServerMutation()
const deleteMutation = useDeleteMcpServerMutation()

// Add server — button morphs into form, mirrors PluginsSection's marketplace-add pattern.
const addExpanded = ref(false)
const addName = ref('')
const addHost = ref('')

function expandAdd() {
  addExpanded.value = true
}

function collapseAdd() {
  addExpanded.value = false
  addName.value = ''
  addHost.value = ''
}

async function submitAdd() {
  if (!addName.value.trim() || !addHost.value.trim()) return
  try {
    await createMutation.mutateAsync({ orgId: props.orgId, name: addName.value.trim(), host: addHost.value.trim() })
    toast.add({ title: t('connectors.toast.serverAdded'), color: 'success' })
    collapseAdd()
  } catch (e) {
    toast.add({ title: t('connectors.toast.serverAddFailed'), description: extractApiError(e, t('connectors.toast.serverAddFailed')), color: 'error' })
  }
}

async function remove(mcpServerId: string) {
  try {
    await deleteMutation.mutateAsync({ orgId: props.orgId, mcpServerId })
    toast.add({ title: t('connectors.toast.serverRemoved'), color: 'success' })
  } catch (e) {
    toast.add({ title: t('connectors.toast.serverRemoveFailed'), description: extractApiError(e, t('connectors.toast.serverRemoveFailed')), color: 'error' })
  }
}

// Which server's auth form is expanded (one at a time).
const authFormOpenFor = ref<string | null>(null)
function toggleAuthForm(id: string) {
  authFormOpenFor.value = authFormOpenFor.value === id ? null : id
}
</script>

<template>
  <section>
    <div class="flex items-center justify-between mb-3">
      <h4 class="text-sm font-semibold text-neutral-900 dark:text-neutral-100">
        {{ t('connectors.title') }}
      </h4>

      <div class="relative h-7 flex items-center">
        <form
          v-if="addExpanded"
          class="flex items-center gap-1.5"
          @submit.prevent="submitAdd"
        >
          <UInput
            v-model="addName"
            :placeholder="t('connectors.namePlaceholder')"
            size="xs"
            class="w-32"
          />
          <UInput
            v-model="addHost"
            :placeholder="t('connectors.urlPlaceholder')"
            size="xs"
            class="w-56"
          />
          <UButton
            type="submit"
            icon="i-lucide-arrow-right"
            size="xs"
            variant="soft"
            :loading="createMutation.isLoading.value"
            :disabled="!addName.trim() || !addHost.trim()"
          />
          <UButton
            icon="i-lucide-x"
            size="xs"
            color="neutral"
            variant="ghost"
            @click="collapseAdd"
          />
        </form>
        <UButton
          v-else
          icon="i-lucide-plus"
          :label="t('connectors.addServer')"
          size="xs"
          color="neutral"
          variant="outline"
          @click="expandAdd"
        />
      </div>
    </div>

    <p class="text-xs text-neutral-500 dark:text-neutral-400 mb-3">
      {{ t('connectors.subtitle') }}
    </p>

    <div
      v-if="isLoading && !servers"
      class="rounded-xl border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 shadow-xs dark:shadow-none p-8 flex items-center justify-center"
    >
      <UIcon
        name="i-lucide-loader-2"
        class="size-5 text-neutral-400 animate-spin"
      />
    </div>

    <div
      v-else-if="!servers?.length"
      class="rounded-xl border border-dashed border-neutral-200 dark:border-neutral-700 p-6 text-center text-xs text-neutral-500"
    >
      {{ t('connectors.empty') }}
    </div>

    <ul
      v-else
      class="rounded-xl border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 shadow-xs dark:shadow-none divide-y divide-neutral-200 dark:divide-neutral-700 overflow-hidden"
    >
      <li
        v-for="server in servers"
        :key="server.id"
        class="px-4 py-2.5"
      >
        <div class="flex items-center justify-between gap-3">
          <div class="min-w-0">
            <div class="flex items-center gap-2">
              <UIcon
                name="i-lucide-plug"
                class="size-3.5 text-neutral-400 shrink-0"
              />
              <span class="text-sm truncate text-neutral-700 dark:text-neutral-300">{{ server.name }}</span>
              <span
                v-if="server.has_credential"
                class="text-[10px] px-1.5 py-0.5 rounded-full bg-neutral-200 dark:bg-neutral-600 text-neutral-700 dark:text-neutral-200 font-medium"
              >{{ t('connectors.authConfigured') }}</span>
            </div>
            <p class="text-xs text-neutral-500 dark:text-neutral-400 truncate font-mono">
              {{ server.host }}
            </p>
          </div>
          <div class="flex items-center gap-1 shrink-0">
            <UButton
              :label="server.has_credential ? t('connectors.editAuth') : t('connectors.addAuth')"
              size="xs"
              variant="soft"
              @click="toggleAuthForm(server.id)"
            />
            <UButton
              icon="i-lucide-trash-2"
              variant="ghost"
              color="error"
              size="xs"
              :loading="deleteMutation.isLoading.value"
              @click="remove(server.id)"
            />
          </div>
        </div>
        <div
          v-if="authFormOpenFor === server.id"
          class="mt-2"
        >
          <AddAuthForm
            :org-id="orgId"
            subject-type="mcp_server"
            :subject-id="server.id"
            @close="authFormOpenFor = null"
          />
        </div>
      </li>
    </ul>
  </section>
</template>
