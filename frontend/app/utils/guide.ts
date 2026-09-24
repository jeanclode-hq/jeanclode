export type GuideView = 'home' | 'pullRequests' | 'issues' | 'gitOrg' | 'sentry' | 'settings'

export interface GuideStep {
  // Also the `data-guide` anchor it spotlights in the real page; a step with no
  // matching anchor shows the page without a spotlight.
  id: string
  // For anchors repeated on every row: spotlight only the first `limit` of
  // them, so the step points at one row instead of the whole list.
  limit?: number
  settingsTab?: 'account' | 'workspace'
}

export interface GuideChapter {
  id: GuideView
  icon: string
  steps: GuideStep[]
}

export const GUIDE_CHAPTERS: GuideChapter[] = [
  {
    id: 'home',
    icon: 'i-lucide-layout-dashboard',
    steps: [{ id: 'welcome' }, { id: 'stats' }, { id: 'topUsers' }, { id: 'live' }],
  },
  {
    id: 'pullRequests',
    icon: 'i-lucide-git-pull-request',
    steps: [{ id: 'prList' }, { id: 'prReview', limit: 1 }, { id: 'prLoop' }, { id: 'prFilters' }],
  },
  {
    id: 'issues',
    icon: 'i-lucide-circle-dot',
    steps: [{ id: 'issueSources' }, { id: 'issueList' }, { id: 'issueFix', limit: 1 }, { id: 'issueMapped' }],
  },
  {
    id: 'gitOrg',
    icon: 'i-lucide-git-branch',
    steps: [
      { id: 'gitRepos' },
      { id: 'gitRelated', limit: 2 },
      { id: 'gitAlwaysInclude' },
      { id: 'gitTrigger' },
      { id: 'gitNotify' },
      { id: 'gitSubgroup' },
      { id: 'gitWebhooks' },
      { id: 'gitTools' },
    ],
  },
  {
    id: 'sentry',
    icon: 'i-lucide-bug',
    steps: [{ id: 'sentryMapping' }, { id: 'sentryAutofix' }, { id: 'sentryBatch' }, { id: 'sentryGate' }, { id: 'sentryImport' }],
  },
  {
    id: 'settings',
    icon: 'i-lucide-settings',
    steps: [
      { id: 'settingsAccounts', settingsTab: 'account' },
      { id: 'settingsMembers', settingsTab: 'workspace' },
      { id: 'done', settingsTab: 'workspace' },
    ],
  },
]

export const GUIDE_STEPS = GUIDE_CHAPTERS.flatMap((chapter) =>
  chapter.steps.map((step) => ({ chapter, step })),
)
