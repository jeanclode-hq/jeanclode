# Adding Frontend Support for an Integration

A backend connection endpoint is useless if no tenant can reach it. There
is **no generic "integrations" component** to plug a new provider into —
every provider's connect form, overview list, and settings panel is
bespoke Vue, and several places hardcode a provider union that must be
extended by hand. Budget real time for this step; it is not a config flag.

**Check `frontend/app/components/onboarding/StepConnectors.vue` first.**
It already lists a `jira` entry in its `connectors` array with
`comingSoon: true` — if the source being added is Jira, this is likely the
exact spot the work is meant to complete, not a new file.

## 1. The integrations page shell

`frontend/app/pages/integrations.vue` (route `/integrations`) is a tabbed
shell driven by a hardcoded union:

```ts
type IntegrationType = 'github' | 'gitlab' | 'sentry'
```

It fetches all orgs once (`useOrgsQuery`) and filters client-side per
provider, builds one tab per provider, and does an explicit
`v-if / v-else-if` chain rendering `{Provider}Overview` (list) or
`{Provider}OrgDetail`/`SentryOrgDetail` (drill-down) based on the selected
org. **Adding a source means adding a branch here** — a new
`IntegrationType` member, a new tab entry, a new `v-else-if` block. There
is no `<IntegrationOverview :provider="...">` to hand this to generically.

Separately:
- `frontend/app/pages/settings.vue` — **per-user** OAuth identity linking
  (not org connection), gated by `ALLOWED_PROVIDERS = ['github', 'gitlab']`.
  Only relevant if the new source has a personal-identity OAuth concept
  (Sentry doesn't; most issue-tracker sources won't either).
- `frontend/app/pages/admin.vue` — **instance-level** admin config
  (OAuth app client id/secret), gated by `ADMIN_SECRET`. Only needed if
  the source requires an instance-wide OAuth app rather than a per-tenant
  API token — matches the backend's admin-vs-token-paste distinction in
  add-backend.md Step 3. Delegates to `components/admin/GitHubSection.vue`
  / `GitLabSection.vue`; a new source with this need adds a `Section`
  entry (`admin.vue`) plus its own section component.

## 2. Connect flow — write a new bespoke component, don't try to generalize an existing one

Each provider's connect flow is a standalone component under
`frontend/app/components/integrations/`:

- **Token-paste form** (`GitLabOverview.vue`, `SentryOverview.vue`) — this
  is the template for most new issue-tracker sources. An inline `<form>`
  collecting the token (+ any org slug / base URL / feature toggles),
  submitted through a mutation composable. **This is almost certainly the
  right shape for Jira**, matching the backend's token-paste connection
  endpoint (add-backend.md Step 3).
- **App-install redirect** (`GitHubOverview.vue`) — no form,
  `window.location.href` to a static install URL, claimed back via
  `useGitHubClaim.ts` after redirect. Only relevant if the source has an
  App-install concept.

Connect mutations live in `frontend/app/composables/queries/onboarding.ts`
(despite the filename, reused outside onboarding too) — e.g.
`useAddGitLabSourceMutation()` wraps the generated `addGitlabSource` SDK
call. Copy this shape exactly for a new source:

```ts
export function useAddJiraSourceMutation() {
  const client = useApi()
  const queryCache = useQueryCache()
  return useMutation({
    mutation: async (vars: {...}) => unwrap(await addJiraSource({ client, body: {...} })),
    onSuccess: () => queryCache.invalidateQueries({ key: ['organizations'] }),
  })
}
```

`unwrap()` (defined locally in `organizations.ts`/`onboarding.ts`) throws
on `result.error` since the generated hey-api client resolves non-2xx
responses as `{ error }` rather than rejecting — always route generated
calls through it. The client itself comes from `useApi()`
(`frontend/app/composables/useApi.ts`, sets `throwOnError: true` and the
SSR-safe base URL via `useApiBase()` — never read
`runtimeConfig.public.apiBase` directly).

## 3. Org settings UI

Settings read/write through **generic** composables regardless of
provider — no new plumbing needed here, just a new form:

- `useOrgSettingsQuery(orgId)` → GET, returns the backend's discriminated
  `GitOrgSettings | SentryOrgSettings` union (components cast as needed).
- `useUpdateOrgSettingsMutation()` → PATCH, **partial per field** — each
  setting has its own small `update*` handler calling
  `settingsMutation.mutateAsync({ orgId, settings: { <field>: value } } )`,
  not one big form submit.

`SentryOrgDetail.vue` is the concrete template for a new source's settings
panel (batch window/size, trigger mode, a gate toggle) — one
`USelect`/`USwitch` per setting, each with its own async handler and a
try/catch + toast. Extend `backend/api/models/settings.py`'s
`<Name>OrgSettings` first (add-backend.md Step 2), then mirror this
component's structure.

## 4. Project/repo mapping UI

If the new source has "projects" that map to git repos (true for Jira, per
add-backend.md's `Repository`/`RepositoryMapping` modeling), reuse:
- **`RepoMapPicker.vue`** — genuinely provider-agnostic, takes `OrgResponse[]`.
- The paginate/filter/search table pattern in `SentryOrgDetail.vue`'s
  "Project Mappings" section — each unmapped row shows a `RepoMapPicker`,
  each mapped row shows a chip + `mapping_method` badge
  (`code_mapping`/`fuzzy`/`manual`).
- `useUpdateSourceProjectMutation()` / `useResolveProjectMappingsMutation()`
  as the mutation template — swap for the new source's
  `listSourceProjects`/`updateSourceProject`-equivalent generated endpoints.

## 5. Places that hardcode a provider union — update every one, there is no single source of truth

None of these are generated from the backend; each is a separate
hand-maintained literal union in the frontend:

- `frontend/app/types/api.ts` — `type Provider = 'github' | 'gitlab' | 'sentry'`
- `frontend/app/pages/integrations.vue` — `IntegrationType`
- `frontend/app/pages/settings.vue` — `ALLOWED_PROVIDERS` / `OAuthProvider`
  (only if the source needs personal OAuth identity)
- `frontend/app/composables/useAuth.ts` — `OAuthProvider`
- `frontend/app/utils/provider-icons.ts` — `PROVIDER_ICONS` map (add a
  `jira` entry with `lightSrc`/`darkSrc`/`fallback`, consumed via
  `getProviderIcon()` and the shared `<ProviderIcon>` component — this one
  genuinely is reusable, just needs a new entry)
- `packages/api-types` `ProviderConfig` type (`appConfig.providers`) — only
  has `github_enabled`/`gitlab_enabled`/`llm_enabled` today; a
  toggleable-per-instance source needs a new backend-generated field here,
  consumed via `useAppConfigQuery()`

Miss one of these and the new source will half-work — e.g. connect fine
but not render its icon, or work in `integrations.vue` but still be
rejected by `settings.vue`'s allowlist if a personal-identity flow is
added later.

## 6. i18n

All user-facing strings belong in `frontend/app/locales/en.json`
(`$t('...')` in templates, `useI18n().t('...')` in script) — see the
`integrations.gitlab.*` block for the convention. Note this is
**inconsistently applied today**: `GitHubOverview.vue` and
`SentryOverview.vue` both hardcode plain English strings directly in their
templates rather than through `en.json`. Follow the documented convention
for new code regardless — don't copy the hardcoded-string shortcut just
because existing components do it.

## Checklist

- [ ] Checked `StepConnectors.vue` for an existing `comingSoon` stub before
      writing a new component from scratch
- [ ] New branch added to `integrations.vue`'s provider union + tab list +
      `v-else-if` chain
- [ ] Connect-flow component written matching the backend's actual auth
      shape (token-paste vs App-install vs OAuth) — don't default to
      copying GitHub's pattern for a token-based source
- [ ] Mutation composable added to `queries/onboarding.ts` (or a new
      `queries/<name>.ts`) following the `unwrap()` + `useMutation` +
      `queryCache.invalidateQueries` idiom
- [ ] Settings panel added/extended for the new `<Name>OrgSettings` fields,
      one `USelect`/`USwitch` per field with its own async handler
- [ ] Project/repo mapping UI added if applicable, reusing `RepoMapPicker`
      and the Sentry project-mappings table pattern
- [ ] Every hardcoded provider union updated (§5) — `types/api.ts`,
      `integrations.vue`, `useAuth.ts`, `provider-icons.ts`, and
      `settings.vue` if personal OAuth identity applies
- [ ] New user-facing strings added to `app/locales/en.json`, not inlined
- [ ] `make generate-types` run first so the new SDK functions exist
      before wiring composables to them
- [ ] `pnpm typecheck` and `pnpm lint` pass
