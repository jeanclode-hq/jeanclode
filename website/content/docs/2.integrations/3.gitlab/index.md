---
title: GitLab
description: Set up GitLab OAuth and webhooks for merge request reviews and issue automation.
icon: i-lucide-gitlab
navigation:
  title: GitLab
seoTitle: "GitLab Integration — OAuth and Webhooks | JeanClode"
seoDescription: "Set up GitLab OAuth and webhooks so JeanClode reviews merge requests, resolves issues and work items, and pushes fixes to your GitLab projects."
---

JeanClode needs two things from GitLab:

1. An **OAuth application** so people can sign in to the dashboard. Same for everyone.
2. **Webhooks + an access token** so it can see your repositories and react to merge requests, issues and mentions. This part depends on your GitLab plan.

Set up the OAuth app first, then follow the one tutorial that matches your plan:

- [**GitLab Free**](/docs/integrations/gitlab/free) — group webhooks aren't available, so you use an instance system hook plus a webhook per project.
- [**GitLab Premium / Ultimate**](/docs/integrations/gitlab/premium) — one group webhook covers everything.

If you're not sure, you're almost certainly on Free.

---

## Step 1: OAuth application

This powers *"Sign in with GitLab"* on the dashboard and nothing else — it never touches your repositories.

Create it at the level that fits your setup:

- **User-level**: [gitlab.com/-/profile/applications](https://gitlab.com/-/profile/applications)
- **Group-level**: `Settings > Applications` in your GitLab group
- **Instance-level** (self-hosted): `Admin > Applications`

| Setting | Value |
|---------|-------|
| Name | `JeanClode` |
| Redirect URI | `https://your-api-domain.com/auth/callback/gitlab` |
| Confidential | Yes |
| Scopes | `read_user`, `read_api` |

Save, then go to `/admin` → **GitLab** and enter the **Application ID**, **Secret**, and **Instance URL** (leave blank for `gitlab.com`).

While you're on that page, set a **webhook secret** — one value, e.g. `openssl rand -hex 32`. You'll reuse this exact string as the *secret token* on every webhook and system hook in the tutorials. Incoming deliveries are matched on that token, not on the URL.

::details{summary="Override with environment variables instead"}

Environment variables take precedence over the admin page configuration.

```bash
GITLAB_CLIENT_ID=<application ID>
GITLAB_CLIENT_SECRET=<application secret>
GITLAB_INSTANCE_URL=https://gitlab.com  # or your self-hosted URL
GITLAB_WEBHOOK_SECRET=<your webhook secret>
```

::

---

## Which tutorial do I follow?

Webhooks are the only thing that differs by plan:

| | [**GitLab Free**](/docs/integrations/gitlab/free) | [**GitLab Premium / Ultimate**](/docs/integrations/gitlab/premium) |
|---|---|---|
| Group webhooks | Not available | Available |
| What you set up | An instance **system hook** + JeanClode manages a webhook per project | One **group webhook**, by hand |
| Token role | **Maintainer** (so JeanClode can create the project webhooks) | **Developer** |

---

## Access token scopes (reference)

Both tutorials use the same scopes:

| Scope | Used for |
|-------|----------|
| `api` | Merge request creation, notes and discussions, issue reads, pipeline and job-log reads, member lookups, webhook management |
| `read_repository` | `git clone` over HTTP |
| `write_repository` | `git push` over HTTP |

For **personal** access tokens, `api` alone already covers Git-over-HTTP. **Group and project** tokens don't, so `read_repository` / `write_repository` must be selected explicitly.

## Who can trigger the bot

Separate from the token above — this is about the *person* doing the mentioning.

Mentioning `@jeanclode-bot` on a merge request or issue only dispatches the respond workflow if that person has at least **Developer** access on the project. A Reporter, a Guest, or someone who isn't a member at all is ignored — they can't change the code themselves, so they can't have the bot do it for them.

The check is fail-closed: if the membership lookup can't be completed, the mention is not acted on. This is symmetric with the `write`/`triage` threshold applied on GitHub.
