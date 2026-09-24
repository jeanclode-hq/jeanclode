import type {
  GitOrgSettings,
  McpServerResponse,
  OrgMembersResponse,
  OrgResponse,
  OrgSubgroupsResponse,
  PluginsOverview,
  RelatedRepo,
  RepoPage,
  RepoResponse,
  SentryOrgSettingsOutput,
  SentryProjectResponse,
} from '@jeanclode/api-types'
import type {
  Issue,
  Organization,
  PaginatedResponse,
  PullRequest,
  RepoOptionPage,
  WorkspaceExecution,
  WorkspaceMembers,
  WorkspaceSources,
  WorkspaceStats,
} from '~/types/api'

// The guide renders the real pages and settings components against these ids.
// Every request carrying one is answered here and never reaches the backend,
// so the guide looks the same on an empty workspace and can't write anything.
const MARKER = 'guide-demo'
export const DEMO_WORKSPACE_ID = `${MARKER}-workspace`

const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString()

const gitOrgId = `${MARKER}-gitlab`
const sentryOrgId = `${MARKER}-sentry`

export const DEMO_GIT_ORG: OrgResponse = {
  id: gitOrgId,
  name: 'acme',
  provider: 'gitlab',
  external_org_id: 'acme',
  avatar_url: null,
  onboarding_step: 'complete',
  repo_count: 4,
  created_at: ago(60 * 24 * 30),
}

export const DEMO_SENTRY_ORG: Organization = {
  id: sentryOrgId,
  name: 'acme',
  provider: 'sentry',
  external_org_id: 'acme',
  base_url: null,
  avatar_url: null,
  onboarding_step: 'complete',
  repo_count: 3,
  created_at: ago(60 * 24 * 30),
}

function repo(slug: string, enabled = true, related: string[] = []): RepoResponse {
  return {
    id: `${MARKER}-repo-${slug}`,
    org_id: gitOrgId,
    root_org_id: gitOrgId,
    name: `acme/shop/${slug}`,
    external_id: slug,
    provider: 'gitlab',
    web_url: null,
    avatar_url: null,
    enabled,
    related: related.map((r) => ({ id: `${MARKER}-repo-${r}`, root_org_id: gitOrgId, name: `acme/shop/${r}` })),
  }
}

const repos = [repo('api', true, ['web']), repo('web', true, ['api']), repo('worker'), repo('docs', false)]

const stats: WorkspaceStats = {
  window_days: 30,
  dashboard: {
    running: 2,
    queued: 1,
    successful_runs: 48,
    reviewed_prs: 31,
    top_users: [
      { identity_id: `${MARKER}-alice`, username: 'alice', avatar_url: null, provider: 'gitlab', pings: 14 },
      { identity_id: `${MARKER}-bob`, username: 'bob', avatar_url: null, provider: 'gitlab', pings: 9 },
    ],
  },
  issues: { handled: 23, prs_created: 17, prs_merged: 12 },
  pull_requests: { reviewed: 31, pending_review: 4, summarized: 27 },
}

function execution(
  id: string,
  title: string,
  workflow: WorkspaceExecution['workflow'],
  status: WorkspaceExecution['status'],
  source: string,
  repoSlug: string,
  minutes: number,
): WorkspaceExecution {
  return {
    id: `${MARKER}-exec-${id}`,
    issue_title: title,
    source,
    source_name: 'acme',
    source_avatar_url: null,
    repo_name: `acme/shop/${repoSlug}`,
    workflow,
    kind: workflow === 'review' || workflow === 'summary' ? 'pull_request' : 'issue',
    status,
    error_type: null,
    error_detail: null,
    pr_url: null,
    pr_number: null,
    prompt_text: null,
    started_at: ago(minutes),
    completed_at: null,
    duration_seconds: null,
  }
}

const activeExecutions = [
  execution('1', 'TypeError: Cannot read properties of undefined (reading \'total\')', 'fix', 'running', 'sentry', 'api', 4),
  execution('2', 'feat: export invoices as CSV', 'review', 'running', 'gitlab', 'web', 2),
  execution('3', 'Dark mode breaks the invoice PDF', 'issue_resolve', 'queued', 'gitlab', 'web', 1),
]

function pullRequest(
  n: number,
  title: string,
  repoSlug: string,
  status: PullRequest['status'],
  executionStatus: PullRequest['execution_status'],
  workflow: PullRequest['workflow'],
  minutes: number,
): PullRequest {
  return {
    id: `${MARKER}-pr-${n}`,
    title,
    author: viewer?.username ?? 'jeanclode-bot',
    pr_url: '#',
    pr_number: n,
    branch_name: `jeanclode/${n}`,
    repo_name: `acme/shop/${repoSlug}`,
    provider: viewer?.provider ?? 'gitlab',
    status,
    execution_status: executionStatus,
    execution_id: `${MARKER}-exec-pr-${n}`,
    workflow,
    error_type: null,
    error_detail: null,
    execution_trigger: null,
    execution_started_at: ago(minutes),
    repo_enabled: true,
    created_at: ago(minutes + 30),
    merged_at: null,
  }
}

// The PR page only enables Review for the PR's author, so the example PRs are
// attributed to whoever is viewing the guide.
let viewer: { provider: 'github' | 'gitlab', username: string } | null = null

export function setGuideDemoViewer(user: { github_username: string | null, gitlab_username: string | null } | null) {
  viewer = user?.gitlab_username
    ? { provider: 'gitlab', username: user.gitlab_username }
    : user?.github_username
      ? { provider: 'github', username: user.github_username }
      : null
}

const pullRequests = () => [
  // Idle first: the "Get a review" step spotlights the first row's button,
  // and a running review shows a spinner there instead of Review.
  pullRequest(409, 'feat: export invoices as CSV', 'web', 'open', 'none', null, 45),
  pullRequest(412, 'fix: guard empty cart total in checkout', 'api', 'open', 'running', 'review', 3),
  pullRequest(405, 'fix: retry webhook deliveries on 502', 'worker', 'open', 'completed', 'respond', 120),
  pullRequest(398, 'chore: bump pydantic to 2.9', 'api', 'merged', 'completed', 'summary', 60 * 26),
]

function issue(
  n: number,
  title: string,
  source: string,
  result: Issue['result'],
  executionStatus: string,
  workflow: Issue['workflow'],
  minutes: number,
): Issue {
  return {
    id: `${MARKER}-issue-${n}`,
    external_id: String(n),
    title,
    culprit: null,
    level: 'error',
    status: 'open',
    execution_status: executionStatus,
    execution_id: executionStatus === 'none' ? null : `${MARKER}-exec-issue-${n}`,
    result,
    event_count: source === 'sentry' ? 120 + n : 0,
    first_seen: ago(minutes + 600),
    last_seen: ago(minutes),
    author: source === 'sentry' ? null : 'alice',
    source,
    source_org_id: source === 'sentry' ? sentryOrgId : gitOrgId,
    project: source === 'sentry' ? 'checkout-backend' : 'acme/shop/web',
    issue_url: null,
    triage_result: null,
    workflow,
    has_mapping: true,
    repo_enabled: true,
    created_at: ago(minutes + 600),
  }
}

const issues = [
  issue(1, 'KeyError: \'currency\' in invoice totals', 'sentry', 'pr_open', 'completed', 'fix', 20),
  issue(2, 'TypeError: Cannot read properties of undefined (reading \'total\')', 'sentry', 'running', 'running', 'fix', 4),
  issue(3, 'Dark mode breaks the invoice PDF', 'gitlab', 'running', 'queued', 'issue_resolve', 1),
  issue(4, 'TimeoutError in nightly sync job', 'sentry', 'pending', 'none', null, 90),
  issue(5, 'Allow SSO for admin accounts', 'gitlab', 'pending', 'none', null, 300),
]

function paginated<T>(objects: T[]): PaginatedResponse<T> {
  return { objects, pagination: { total: objects.length, limit: 25, page: 1 }, meta: { timestamp: 0 } }
}

const sources: WorkspaceSources = {
  sources: [
    { id: gitOrgId, name: 'acme', provider: 'gitlab', avatar_url: null },
    { id: sentryOrgId, name: 'acme', provider: 'sentry', avatar_url: null },
  ],
}

const repoOptions: RepoOptionPage = {
  items: repos.map((r) => ({ id: r.id, name: r.name, org_name: 'acme' })),
  total: repos.length,
  page: 1,
  has_more: false,
}

const members: WorkspaceMembers = {
  members: [
    { user_id: `${MARKER}-alice`, username: 'alice', avatar_url: null, providers: ['github', 'gitlab'], joined_at: ago(60 * 24 * 30) },
    { user_id: `${MARKER}-bob`, username: 'bob', avatar_url: null, providers: ['gitlab'], joined_at: ago(60 * 24 * 12) },
  ],
  total: 2,
  page: 1,
  has_more: false,
}

const gitSettings: GitOrgSettings = {
  trigger_permission: 'developer_only',
  notify: { on_ready: [`${MARKER}-alice`, `${MARKER}-bob`] },
  related_repos: { always_include: [repos[3]!.id], pack_subgroup: true, excluded_subgroups: [] },
  manage_project_webhooks: false,
}

const sentrySettings: SentryOrgSettingsOutput = {
  triggers: { triage: 'automatic' },
  batch_window: '1h',
  batch_size: '5',
  gate_on_open_fix_prs: true,
  backfill: '30d',
}

const orgMembers: OrgMembersResponse = {
  members: ['alice', 'bob', 'carol'].map((username) => ({
    provider_identity_id: `${MARKER}-${username}`,
    username,
    avatar_url: null,
    provider: 'gitlab',
    role: 'developer',
    has_account: username !== 'carol',
  })),
  total: 3,
}

const subgroups: OrgSubgroupsResponse = {
  items: [{ id: `${MARKER}-subgroup-shop`, name: 'shop', repo_count: 4 }],
  max_pack_size: 50,
}

const repoPage: RepoPage = {
  items: repos,
  total: repos.length,
  enabled_count: repos.filter((r) => r.enabled).length,
  page: 1,
  has_more: false,
}

const sentryProjects: SentryProjectResponse[] = [
  { id: `${MARKER}-project-1`, external_id: '1', name: 'checkout-backend', mapped_repo_id: repos[0]!.id, mapping_method: 'code_mapping' },
  { id: `${MARKER}-project-2`, external_id: '2', name: 'storefront', mapped_repo_id: repos[1]!.id, mapping_method: 'manual' },
  { id: `${MARKER}-project-3`, external_id: '3', name: 'sync-worker', mapped_repo_id: repos[2]!.id, mapping_method: 'fuzzy' },
]

const plugins: PluginsOverview = { marketplaces: [], installed: [] }

const mcpServers: McpServerResponse[] = [
  { id: `${MARKER}-mcp-1`, org_id: gitOrgId, name: 'linear', host: 'mcp.linear.app', has_credential: true },
]

const ROUTES: [RegExp, (url: URL) => unknown][] = [
  [/\/workspaces\/[^/]+\/stats$/, () => stats],
  [/\/workspaces\/[^/]+\/sources$/, () => sources],
  [/\/workspaces\/[^/]+\/executions\/active$/, () => ({ items: activeExecutions })],
  [/\/workspaces\/[^/]+\/pull-requests\/repositories$/, () => repoOptions],
  [/\/workspaces\/[^/]+\/pull-requests$/, () => paginated(pullRequests())],
  [/\/workspaces\/[^/]+\/members$/, () => members],
  [/\/issues\/repositories$/, () => repoOptions],
  [/\/issues$/, () => paginated(issues)],
  [/\/organizations$/, (url) => {
    const wanted = url.searchParams.getAll('provider')
    return [DEMO_GIT_ORG, DEMO_SENTRY_ORG].filter((o) => !wanted.length || wanted.includes(o.provider))
  }],
  [/\/organizations\/[^/]+\/settings$/, (url) => (url.pathname.includes(sentryOrgId) ? sentrySettings : gitSettings)],
  [/\/organizations\/[^/]+\/members$/, () => orgMembers],
  [/\/organizations\/[^/]+\/subgroups$/, () => subgroups],
  [/\/repos\/lookup$/, (url) => {
    const ids = url.searchParams.getAll('ids')
    return repos.filter((r) => ids.includes(r.id)).map<RelatedRepo>((r) => ({ id: r.id, root_org_id: r.root_org_id, name: r.name }))
  }],
  [/\/repos$/, () => repoPage],
  [/\/sources\/sentry\/projects$/, () => sentryProjects],
  [/\/plugins$/, () => plugins],
  [/\/mcp-servers$/, () => mcpServers],
]

export function guideDemoResponse(request: Request): Response | null {
  if (!request.url.includes(MARKER)) return null
  const url = new URL(request.url)
  // The guide renders its scene inert, so anything but a read is a bug; answer
  // it anyway rather than let a demo id reach the backend.
  const handler = request.method === 'GET'
    ? ROUTES.find(([pattern]) => pattern.test(url.pathname))?.[1]
    : undefined
  const body = handler ? handler(url) : {}
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
}
