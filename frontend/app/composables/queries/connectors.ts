import {
  createMcpServer,
  deleteCredential,
  deleteMcpServer,
  listCredentials,
  listMcpServers,
  updateMcpServer,
  writeCredential,
} from '@jeanclode/api-types'
import type { AuthType, SubjectType, WriteCredentialRequest } from '@jeanclode/api-types'

const mcpServersKey = (orgId: string) => ['mcp-servers', orgId] as const
const credentialsKey = (subjectType: SubjectType, subjectId: string) => ['credentials', subjectType, subjectId] as const

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
      queryCache.invalidateQueries({ key: credentialsKey('mcp_server', vars.mcpServerId) })
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
// generic "add auth" mechanism for both, per issue #191. A subject holds
// one credential per target host.

function invalidateSubject(
  queryCache: ReturnType<typeof useQueryCache>,
  vars: { orgId: string, subjectType: SubjectType, subjectId: string },
) {
  queryCache.invalidateQueries({ key: credentialsKey(vars.subjectType, vars.subjectId) })
  if (vars.subjectType === 'mcp_server') {
    queryCache.invalidateQueries({ key: mcpServersKey(vars.orgId) })
  } else {
    // the plugins overview inlines each install's credentials
    queryCache.invalidateQueries({ key: pluginsOverviewKey(vars.orgId) })
  }
}

export function useCredentialsQuery(
  orgId: MaybeRefOrGetter<string>,
  subjectType: SubjectType,
  subjectId: MaybeRefOrGetter<string>,
) {
  const client = useApi()

  return useQuery({
    key: () => credentialsKey(subjectType, toValue(subjectId)),
    query: async () => {
      const { data } = await listCredentials({
        client,
        query: { org_id: toValue(orgId), subject_type: subjectType, subject_id: toValue(subjectId) },
      })
      return data ?? []
    },
    enabled: () => !!toValue(subjectId) && !!toValue(orgId),
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
      credentialId?: string
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
          credential_id: vars.credentialId ?? null,
          auth_type: vars.authType,
          secret: vars.secret,
          settings: vars.settings,
        },
      })
      return data!
    },
    onSuccess(_data, vars) {
      invalidateSubject(queryCache, vars)
    },
  })
}

export function useDeleteCredentialMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (vars: { orgId: string, subjectType: SubjectType, subjectId: string, credentialId: string }) => {
      await deleteCredential({ client, path: { credential_id: vars.credentialId } })
    },
    onSuccess(_data, vars) {
      invalidateSubject(queryCache, vars)
    },
  })
}
