/**
 * Shared status display helpers for issues, pull requests, and executions.
 */

// -- Source status (external: unresolved/resolved, open/closed) --

const SOURCE_STATUS_COLORS: Record<string, string> = {
  unresolved: 'warning',
  resolved: 'success',
  open: 'success',
  closed: 'error',
  merged: 'primary',
}

const SOURCE_STATUS_LABELS: Record<string, string> = {
  unresolved: 'Unresolved',
  resolved: 'Resolved',
  open: 'Open',
  closed: 'Closed',
  merged: 'Merged',
}

export function getSourceStatusColor(status: string): string {
  return SOURCE_STATUS_COLORS[status] ?? 'neutral'
}

export function getSourceStatusLabel(status: string): string {
  return SOURCE_STATUS_LABELS[status] ?? status
}

// -- Execution status (agent: pending/queued/running/completed/failed) --

const EXEC_STATUS_COLORS: Record<string, string> = {
  pending: 'neutral',
  queued: 'info',
  running: 'info',
  completed: 'success',
  failed: 'error',
  cancelled: 'neutral',
  none: 'neutral',
  pr_open: 'info',
  pr_merged: 'success',
  not_actionable: 'neutral',
  rejected: 'error',
}

const EXEC_STATUS_LABELS: Record<string, Record<string, string>> = {
  fix: {
    pending: 'Pending',
    queued: 'Queued',
    running: 'Fixing',
    completed: 'Completed',
    failed: 'Failed',
    cancelled: 'Cancelled',
    none: 'Pending',
    pr_open: 'PR Open',
    pr_merged: 'PR Merged',
    not_actionable: 'Not Actionable',
    rejected: 'Rejected',
  },
  review: {
    pending: 'Pending',
    queued: 'Queued',
    running: 'Reviewing',
    completed: 'Completed',
    failed: 'Failed',
    cancelled: 'Cancelled',
    none: 'Pending',
  },
  summary: {
    pending: 'Pending',
    queued: 'Queued',
    running: 'Summarizing',
    completed: 'Completed',
    failed: 'Failed',
    cancelled: 'Cancelled',
    none: 'Pending',
  },
  issue_resolve: {
    pending: 'Pending',
    queued: 'Queued',
    running: 'Resolving',
    completed: 'Completed',
    failed: 'Failed',
    cancelled: 'Cancelled',
    none: 'Pending',
    pr_open: 'PR Open',
    pr_merged: 'PR Merged',
    not_actionable: 'Not Actionable',
    rejected: 'Rejected',
  },
  respond: {
    pending: 'Pending',
    queued: 'Queued',
    running: 'Responding',
    completed: 'Completed',
    failed: 'Failed',
    cancelled: 'Cancelled',
    none: 'Pending',
  },
}

export function getExecStatusColor(status: string): string {
  return EXEC_STATUS_COLORS[status] ?? 'neutral'
}

export function getExecStatusLabel(status: string, workflow: string = 'fix'): string {
  const labels = EXEC_STATUS_LABELS[workflow] ?? EXEC_STATUS_LABELS.fix
  return labels[status] ?? EXEC_STATUS_LABELS.fix[status] ?? status
}

const EXEC_STATUS_ICONS: Record<string, string> = {
  pending: 'i-lucide-clock',
  queued: 'i-lucide-clock',
  running: 'i-lucide-loader-2',
  completed: 'i-lucide-check-circle-2',
  failed: 'i-lucide-x-circle',
  cancelled: 'i-lucide-ban',
  none: 'i-lucide-clock',
  pr_open: 'i-lucide-git-pull-request',
  pr_merged: 'i-lucide-check-circle-2',
  not_actionable: 'i-lucide-circle-slash',
  rejected: 'i-lucide-x-circle',
}

export function getExecStatusIcon(status: string): string {
  return EXEC_STATUS_ICONS[status] ?? 'i-lucide-circle-dot'
}

// -- Workflow display helpers --

const WORKFLOW_META: Record<string, { icon: string, label: string, color: string, bg: string }> = {
  fix: { icon: 'i-lucide-wrench', label: 'Fix', color: 'text-blue-500', bg: 'bg-blue-50 dark:bg-blue-500/10' },
  review: { icon: 'i-lucide-scan-eye', label: 'Review', color: 'text-violet-500', bg: 'bg-violet-50 dark:bg-violet-500/10' },
  summary: { icon: 'i-lucide-file-text', label: 'Summary', color: 'text-amber-500', bg: 'bg-amber-50 dark:bg-amber-500/10' },
  issue_resolve: { icon: 'i-lucide-hammer', label: 'Resolve', color: 'text-emerald-500', bg: 'bg-emerald-50 dark:bg-emerald-500/10' },
  respond: { icon: 'i-lucide-reply', label: 'Respond', color: 'text-cyan-500', bg: 'bg-cyan-50 dark:bg-cyan-500/10' },
}

const DEFAULT_WORKFLOW = { icon: 'i-lucide-cog', label: 'Agent', color: 'text-neutral-500', bg: 'bg-neutral-100 dark:bg-neutral-500/10' }

export function getWorkflowIcon(workflow: string): string {
  return (WORKFLOW_META[workflow] ?? DEFAULT_WORKFLOW).icon
}

export function getWorkflowBg(workflow: string): string {
  return (WORKFLOW_META[workflow] ?? DEFAULT_WORKFLOW).bg
}

export function getWorkflowLabel(workflow: string): string {
  return (WORKFLOW_META[workflow] ?? DEFAULT_WORKFLOW).label
}

export function getWorkflowColor(workflow: string): string {
  return (WORKFLOW_META[workflow] ?? DEFAULT_WORKFLOW).color
}

// -- Issue source helpers --

const GIT_SOURCES = new Set(['github', 'gitlab'])

export function isGitIssue(source: string): boolean {
  return GIT_SOURCES.has(source)
}
