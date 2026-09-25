// Types for backend endpoints not yet in the OpenAPI spec.
// As routes are added to the backend, these will be replaced by
// generated types from @jeanclode/api-types.

// -- Enums --

export type IssueStatus
  = 'pending'
    | 'running'
    | 'pr_open'
    | 'pr_merged'
    | 'not_actionable'
    | 'rejected'
    | 'failed'
    | 'completed'

export type Provider = 'github' | 'gitlab' | 'sentry'

export type OnboardingStep = 'git_provider' | 'connectors' | 'complete'

export type MappingMethod = 'code_mapping' | 'fuzzy' | 'manual'

export type ExecutionStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'none'

export type ExecutionWorkflow = 'fix' | 'review' | 'summary' | 'issue_resolve' | 'respond'

// -- Resources --

export interface Workspace {
  id: string
  name: string
  slug: string
  created_at: string
  updated_at: string
}

export interface Organization {
  id: string
  name: string
  provider: Provider
  external_org_id: string
  base_url: string | null
  avatar_url: string | null
  onboarding_step: OnboardingStep
  repo_count: number
  created_at: string
}

export interface TriageResult {
  actionable: boolean
  reason: string
  confidence: number
  affected_files: string[]
  root_cause_hypothesis: string
  category: string
  existing_pr_url: string | null
  previously_attempted: boolean
  repo_url: string | null
  commit_sha: string | null
}

export interface PullRequestSummary {
  id: string
  pr_number: number
  title: string
  state: PRStatus
  pr_url: string
}

export interface ExecutionSummary {
  id: string
  workflow: ExecutionWorkflow
  trigger: string
  status: ExecutionStatus
  container_id: string | null
  error_type: string | null
  error_detail: string | null
  created_at: string
  fixer_llm_credential?: string | null
  fixer_llm_model?: string | null
  fixer_llm_reason?: string | null
  pull_requests: PullRequestSummary[]
}

export interface Issue {
  id: string
  external_id: string
  title: string
  culprit: string | null
  level: string
  status: string
  execution_status: string
  execution_id: string | null
  result: IssueStatus
  event_count: number
  first_seen: string | null
  last_seen: string | null
  author: string | null
  source: string
  source_org_id: string
  project: string
  issue_url: string | null
  triage_result: string | null
  workflow: ExecutionWorkflow | null
  has_mapping: boolean
  repo_enabled: boolean
  created_at: string
}

export interface IssueDetail extends Issue {
  triage_metadata: TriageResult | null
  executions: ExecutionSummary[]
}

export type PRStatus = 'open' | 'merged' | 'closed'

export interface PullRequest {
  id: string
  title: string
  author: string
  pr_url: string
  pr_number: number
  branch_name: string
  repo_name: string
  provider: string
  status: PRStatus
  execution_status: ExecutionStatus
  execution_id: string | null

  workflow: ExecutionWorkflow | null
  error_type: string | null
  error_detail: string | null
  execution_trigger: string | null
  execution_started_at: string | null
  repo_enabled: boolean
  created_at: string
  merged_at: string | null
}

export interface SourceSummary {
  id: string
  name: string
  provider: string
  avatar_url: string | null
}

export interface WorkspaceSources {
  sources: SourceSummary[]
}

export interface ProjectMapping {
  id: string
  external_id: string
  name: string
  mapped_repo_id: string | null
  mapping_method: MappingMethod | null
}

export interface WorkspaceExecution {
  id: string
  issue_title: string
  source: string
  source_name: string | null
  source_avatar_url: string | null
  repo_name: string | null
  workflow: ExecutionWorkflow
  kind: 'issue' | 'pull_request' | 'unknown'
  status: ExecutionStatus

  error_type: string | null
  error_detail: string | null
  pr_url: string | null
  pr_number: number | null
  prompt_text: string | null
  started_at: string
  completed_at: string | null
  duration_seconds: number | null
}

export interface WorkspaceExecutions {
  items: WorkspaceExecution[]
  total: number
  page: number
  has_more: boolean
}

export interface OrgStats {
  total_issues: number
  issues_fixing: number
  issues_fixed: number
  issues_failed: number
  fix_success_rate: number
  repo_count: number
  total_prs: number
  prs_reviewed: number
  prs_open: number
  latest_title: string | null
  latest_pr_url: string | null
  latest_pr_number: number | null
  latest_at: string | null
}

export interface LgtmEntry {
  username: string
  avatar_url: string | null
  lgtm_count: number
}

export interface PrAuthorEntry {
  username: string
  avatar_url: string | null
  pr_count: number
}

export interface ActiveRepoEntry {
  name: string
  provider: string
  execution_count: number
}

export interface Leaderboards {
  lgtm: LgtmEntry[]
  top_pr_authors: PrAuthorEntry[]
  active_repos: ActiveRepoEntry[]
}

export interface WorkspaceMember {
  user_id: string
  username: string
  avatar_url: string | null
  providers: string[]
  joined_at: string
}

export interface WorkspaceMembers {
  members: WorkspaceMember[]
  total: number
  page: number
  has_more: boolean
}

export interface TopUser {
  identity_id: string
  username: string | null
  avatar_url: string | null
  provider: Provider
  pings: number
}

export interface WorkspaceStats {
  window_days: number
  dashboard: {
    running: number
    queued: number
    successful_runs: number
    reviewed_prs: number
    top_users: TopUser[]
  }
  issues: {
    handled: number
    prs_created: number
    prs_merged: number
  }
  pull_requests: {
    reviewed: number
    pending_review: number
    summarized: number
  }
}

// -- Pagination --

export interface PaginationMeta {
  total: number
  limit: number
  page: number
}

export interface ListMeta {
  timestamp: number
}

export interface PaginatedResponse<T> {
  objects: T[]
  meta: ListMeta
  pagination: PaginationMeta
}

export interface ListResponse<T> {
  objects: T[]
  meta: ListMeta
}

// -- Query params --

export interface ListQuery {
  limit?: number
  page?: number
  search?: string
  order_by?: string[]
}

export interface IssueFilters extends ListQuery {
  workspace_id?: string
  status?: string
  source?: string
  source_org_id?: string
  repository_id?: string
  author?: string
  period?: string
  mapped_only?: boolean
  execution_status?: string
}

export interface PRFilters extends ListQuery {
  status?: PRStatus
  org_id?: string
  repository_id?: string
  author?: string
  period?: string
  execution_status?: string
}

// -- Repo filter options --

export interface RepoOption {
  id: string
  name: string
  org_name: string
}

export interface RepoOptionPage {
  items: RepoOption[]
  total: number
  page: number
  has_more: boolean
}
