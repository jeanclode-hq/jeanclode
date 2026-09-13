export interface UserProfile {
  id: string
  email: string | null
  created_at: string
  display_username: string
  avatar_url: string | null
  onboarding_step: string | null
  last_workspace_id: string | null
  github_username: string | null
  gitlab_username: string | null
  github_external_id: string | null
  gitlab_external_id: string | null
}
