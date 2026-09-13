import type { BackfillScope } from '@jeanclode/api-types'
import {
  addGitlabSource,
  claimOrganization,
  finalizeWorkspaceOnboarding,
  linkSentrySource,
  resolveProjectMappings,
  updateOrganization,
  updateSourceProject,
} from '@jeanclode/api-types'

/** Throw the error if hey-api returned one instead of data. */
function unwrap<T>(result: { data?: T, error?: unknown }): T {
  if (result.error || !result.data) {
    throw result.error ?? new Error('Request failed')
  }
  return result.data
}

export function useAddGitLabSourceMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (vars: { accessToken: string, gitlabUrl?: string, workspaceId?: string, manageProjectWebhooks?: boolean }) => {
      return unwrap(await addGitlabSource({
        client,
        body: {
          access_token: vars.accessToken,
          gitlab_url: vars.gitlabUrl,
          workspace_id: vars.workspaceId,
          manage_project_webhooks: vars.manageProjectWebhooks,
        },
      }))
    },
    onSuccess() {
      queryCache.invalidateQueries({ key: ['organizations'] })
    },
  })
}

export function useLinkSentrySourceMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (vars: { workspaceId: string, orgSlug: string, authToken: string, clientSecret: string, baseUrl?: string, backfillScope?: BackfillScope }) => {
      return unwrap(await linkSentrySource({
        client,
        body: {
          workspace_id: vars.workspaceId,
          org_slug: vars.orgSlug,
          auth_token: vars.authToken,
          client_secret: vars.clientSecret,
          base_url: vars.baseUrl,
          // Omitted means "leave the stored scope alone" — the backend
          // defaults new orgs to the last 30 days.
          backfill_scope: vars.backfillScope ?? null,
        },
      }))
    },
    onSuccess() {
      queryCache.invalidateQueries({ key: ['organizations'] })
    },
  })
}

export function useResolveProjectMappingsMutation() {
  const client = useApi()

  return useMutation({
    mutation: async (vars: { orgId: string }) => {
      return unwrap(await resolveProjectMappings({
        client,
        query: { org_id: vars.orgId },
      }))
    },
  })
}

export function useUpdateSourceProjectMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (vars: { projectId: string, repoId: string | null }) => {
      return unwrap(await updateSourceProject({
        client,
        path: { project_id: vars.projectId },
        body: { repo_id: vars.repoId },
      }))
    },
    onSuccess() {
      queryCache.invalidateQueries({ key: ['source-projects'] })
    },
  })
}

export function useClaimOrgMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (vars: { installationId: string, workspaceId: string }) => {
      return unwrap(await claimOrganization({
        client,
        body: {
          installation_id: vars.installationId,
          workspace_id: vars.workspaceId,
        },
      }))
    },
    onSuccess() {
      queryCache.invalidateQueries({ key: ['organizations'] })
    },
  })
}

export function useFinalizeOnboardingMutation() {
  const client = useApi()

  return useMutation({
    mutation: async (workspaceId: string) => {
      return unwrap(await finalizeWorkspaceOnboarding({
        client,
        path: { workspace_id: workspaceId },
      }))
    },
  })
}

export function useUpdateOrgStepMutation() {
  const client = useApi()
  const queryCache = useQueryCache()

  return useMutation({
    mutation: async (vars: { orgId: string, onboardingStep: string }) => {
      return unwrap(await updateOrganization({
        client,
        path: { org_id: vars.orgId },
        body: { onboarding_step: vars.onboardingStep },
      }))
    },
    onSuccess() {
      queryCache.invalidateQueries({ key: ['organizations'] })
    },
  })
}
