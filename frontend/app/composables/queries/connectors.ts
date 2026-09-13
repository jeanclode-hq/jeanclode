import {
  createMcpServer,
  deleteCredential,
  deleteMcpServer,
  getCredential,
  listMcpServers,
  updateMcpServer,
  writeCredential,
} from '@jeanclode/api-types'
import type { AuthType, SubjectType, WriteCredentialRequest } from '@jeanclode/api-types'

const mcpServersKey = (orgId: string) => ['mcp-servers', orgId] as const
const credentialKey = (subjectType: SubjectType, subjectId: string) => ['credential', subjectType, subjectId] as const

// -- MCP servers --

export function useMcpServersQuery(orgId: MaybeRefOrGetter<string>) {
  const client = useApi()

  return useQuery({
    key: () => mcpServersKey(toValue(orgId)),
    query: async () => {
      const { data } = await listMcpServers({ client, query: { org_id: toValue(orgId) } })
      return data ?? []
    },
    enabled: () => !!toValue(orgId),
  })
}

export function useCreateMcpServerMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (vars: { orgId: string, name: string, host: string }) => {
      const { data } = await createMcpServer({
        client,
        body: { org_id: vars.orgId, name: vars.name, host: vars.host },
      })
      return data!
    },
    onSuccess(_data, vars) {
      queryCache.invalidateQueries({ key: mcpServersKey(vars.orgId) })
    },
  })
}

export function useDeleteMcpServerMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (vars: { orgId: string, mcpServerId: string }) => {
      await deleteMcpServer({ client, path: { mcp_server_id: vars.mcpServerId } })
    },
    onSuccess(_data, vars) {
      queryCache.invalidateQueries({ key: mcpServersKey(vars.orgId) })
      queryCache.invalidateQueries({ key: credentialKey('mcp_server', vars.mcpServerId) })
    },
  })
}

export function useUpdateMcpServerMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (vars: { orgId: string, mcpServerId: string, name?: string, host?: string }) => {
      const { data } = await updateMcpServer({
        client,
        path: { mcp_server_id: vars.mcpServerId },
        body: { name: vars.name, host: vars.host },
      })
      return data!
    },
    onSuccess(_data, vars) {
      queryCache.invalidateQueries({ key: mcpServersKey(vars.orgId) })
    },
  })
}

// -- Credentials --
// Shared by both mcp_server and plugin_installation subjects — one
// generic "add auth" mechanism for both, per issue #191.

export function useCredentialQuery(subjectType: SubjectType, subjectId: MaybeRefOrGetter<string>) {
  const client = useApi()

  return useQuery({
    key: () => credentialKey(subjectType, toValue(subjectId)),
    query: async () => {
      const { data } = await getCredential({
        client,
        query: { subject_type: subjectType, subject_id: toValue(subjectId) },
      })
      return data ?? null
    },
    enabled: () => !!toValue(subjectId),
  })
}

export function useWriteCredentialMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (vars: {
      orgId: string
      subjectType: SubjectType
      subjectId: string
      authType: AuthType
      secret: WriteCredentialRequest['secret']
      settings: WriteCredentialRequest['settings']
    }) => {
      const { data } = await writeCredential({
        client,
        body: {
          org_id: vars.orgId,
          subject_type: vars.subjectType,
          subject_id: vars.subjectId,
          auth_type: vars.authType,
          secret: vars.secret,
          settings: vars.settings,
        },
      })
      return data!
    },
    onSuccess(_data, vars) {
      queryCache.invalidateQueries({ key: credentialKey(vars.subjectType, vars.subjectId) })
      if (vars.subjectType === 'mcp_server') {
        queryCache.invalidateQueries({ key: mcpServersKey(vars.orgId) })
      } else {
        // the plugins overview inlines each install's credential
        queryCache.invalidateQueries({ key: pluginsOverviewKey(vars.orgId) })
      }
    },
  })
}

export function useDeleteCredentialMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (vars: { orgId: string, subjectType: SubjectType, subjectId: string }) => {
      await deleteCredential({
        client,
        query: { subject_type: vars.subjectType, subject_id: vars.subjectId },
      })
    },
    onSuccess(_data, vars) {
      queryCache.invalidateQueries({ key: credentialKey(vars.subjectType, vars.subjectId) })
      if (vars.subjectType === 'mcp_server') {
        queryCache.invalidateQueries({ key: mcpServersKey(vars.orgId) })
      } else {
        queryCache.invalidateQueries({ key: pluginsOverviewKey(vars.orgId) })
      }
    },
  })
}
