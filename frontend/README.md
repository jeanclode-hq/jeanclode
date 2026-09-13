# Jeanclode Frontend

Nuxt 4 dashboard for managing Jeanclode — workspace management, integration setup (GitHub, GitLab, Sentry), live execution feed, issue/PR tracking, and leaderboards.

## Setup

1. Install dependencies:

```bash
pnpm install
```

2. Configure your backend API URL in `.env`:

```env
NUXT_PUBLIC_API_BASE=http://localhost:8000
```

## Development

Start the development server:

```bash
pnpm dev
```

The app will be available at `http://localhost:3000`

## Project Structure

```
frontend/
├── app/
│   ├── assets/            # CSS and static assets
│   ├── components/        # Vue components (auto-imported)
│   ├── composables/       # Reusable composables (auto-imported)
│   ├── layouts/           # Application layouts (default, auth, blank)
│   ├── locales/           # i18n translations (en.json)
│   ├── middleware/         # Route middleware
│   ├── pages/             # Route pages (file-based routing)
│   ├── stores/            # Pinia store modules (auto-imported)
│   ├── types/             # TypeScript type definitions
│   ├── utils/             # Utility functions (auto-imported)
│   ├── app.config.ts      # NuxtUI design system configuration
│   └── app.vue            # Root component
├── server/                # Nitro server routes (API proxies)
├── eslint.config.mjs      # ESLint configuration
├── nuxt.config.ts         # Nuxt configuration
├── package.json           # Dependencies and scripts
└── tsconfig.json          # TypeScript configuration (strict mode)
```

## Architecture

- **Framework**: Nuxt 4 (Vue 3 with auto-imports and file-based routing)
- **UI Components**: NuxtUI v4 (includes Tailwind v4)
- **State Management**: Pinia + Pinia Colada for async queries
- **i18n**: @nuxtjs/i18n (English only)
- **TypeScript**: Strict mode enabled
- **Code Quality**: ESLint with stylistic rules

## Available Scripts

- `pnpm dev` - Start development server
- `pnpm build` - Build for production
- `pnpm lint` - Lint code
- `pnpm lint:fix` - Lint and auto-fix issues
- `pnpm typecheck` - Run TypeScript type checking
