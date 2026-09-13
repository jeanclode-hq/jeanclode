# Frontend

Nuxt 4 frontend with Vue 3, NuxtUI, Pinia Colada, and i18n.

## Structure

- `app/pages/` - File-based routing
- `app/components/` - Vue components (auto-imported, no prefix)
- `app/composables/` - Reusable composition functions
- `app/stores/` - Pinia stores for global state
- `app/utils/` - Utility functions
- `app/types/` - TypeScript type definitions
- `app/middleware/` - Route middleware
- `app/layouts/` - Layout components (default, auth, blank)
- `app/locales/` - i18n translations (en.json)
- `server/` - Nitro server routes (API proxies)

## Commands

- `pnpm dev` - Start dev server (http://localhost:3000)
- `pnpm build` - Build for production
- `pnpm lint` - Run ESLint
- `pnpm lint:fix` - Fix lint issues
- `pnpm typecheck` - Run TypeScript type checking

## Conventions

- **Package manager**: pnpm
- **State management**: Pinia + Pinia Colada for async queries
- **UI framework**: NuxtUI v4 (includes Tailwind v4)
- **Styling**: 2-space indent, single quotes, no semicolons
- **i18n**: All user-facing strings in `app/locales/en.json`
- **API calls**: Use `useApiBase()` for the backend URL — it returns the
  browser base on the client and the SSR-reachable base (`apiInternal`, set to
  the compose service name in Docker) on the server. Never read
  `runtimeConfig.public.apiBase` directly for a fetch that can run during SSR.
- **Components**: Auto-imported from `app/components/`, no path prefix
