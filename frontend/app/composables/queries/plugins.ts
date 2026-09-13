import {
  connectMarketplace,
  disconnectMarketplace,
  getPluginsOverview,
  installPlugins,
  uninstallPlugin,
  updateInstalledPlugin,
} from '@jeanclode/api-types'
import type { UpdateInstallRequest } from '@jeanclode/api-types'

export function pluginsOverviewKey(orgId: string) {
  return ['plugins', orgId] as const
}

export function usePluginsOverviewQuery(orgId: MaybeRefOrGetter<string>) {
  const client = useApi()

  return useQuery({
    key: () => pluginsOverviewKey(toValue(orgId)),
    query: async () => {
      const { data } = await getPluginsOverview({
        client,
        query: { org_id: toValue(orgId) },
      })
      return data!
    },
    enabled: () => !!toValue(orgId),
  })
}

export function useConnectMarketplaceMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (vars: { orgId: string, gitUrl: string }) => {
      const { data } = await connectMarketplace({
        client,
        body: { org_id: vars.orgId, git_url: vars.gitUrl },
      })
      return data!
    },
    onSuccess(_data, vars) {
      queryCache.invalidateQueries({ key: pluginsOverviewKey(vars.orgId) })
    },
  })
}

export function useDisconnectMarketplaceMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (vars: { orgId: string, marketplaceId: string }) => {
      await disconnectMarketplace({
        client,
        path: { marketplace_id: vars.marketplaceId },
      })
    },
    onSuccess(_data, vars) {
      queryCache.invalidateQueries({ key: pluginsOverviewKey(vars.orgId) })
    },
  })
}

export function useInstallPluginMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (vars: {
      orgId: string
      marketplaceId: string
      pluginNames: string[]
      pinnedRef?: string | null
    }) => {
      const { data } = await installPlugins({
        client,
        body: {
          org_id: vars.orgId,
          marketplace_id: vars.marketplaceId,
          plugin_names: vars.pluginNames,
          pinned_ref: vars.pinnedRef ?? null,
        },
      })
      return data!
    },
    onSuccess(_data, vars) {
      queryCache.invalidateQueries({ key: pluginsOverviewKey(vars.orgId) })
    },
  })
}

export function useUpdateInstalledPluginMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (vars: { orgId: string, installId: string, patch: UpdateInstallRequest }) => {
      const { data } = await updateInstalledPlugin({
        client,
        path: { install_id: vars.installId },
        body: vars.patch,
      })
      return data!
    },
    onSuccess(_data, vars) {
      queryCache.invalidateQueries({ key: pluginsOverviewKey(vars.orgId) })
    },
  })
}

export function useUninstallPluginMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (vars: { orgId: string, installId: string }) => {
      await uninstallPlugin({
        client,
        path: { install_id: vars.installId },
      })
    },
    onSuccess(_data, vars) {
      queryCache.invalidateQueries({ key: pluginsOverviewKey(vars.orgId) })
    },
  })
}
