// https://nuxt.com/docs/api/configuration/nuxt-config
export default defineNuxtConfig({
  modules: [
    '@nuxt/eslint',
    '@nuxt/ui',
    '@pinia/nuxt',
    '@pinia/colada-nuxt',
    '@nuxtjs/i18n',
  ],

  // Component configuration
  components: [
    {
      path: '~/components',
      pathPrefix: false,
    },
  ],

  // Auto-import configuration
  imports: {
    dirs: [
      'composables/**',
      'stores/**',
      'utils/**',
    ],
  },

  devtools: { enabled: true },

  // App configuration
  app: {
    head: {
      title: 'Jeanclode',
      link: [
        { rel: 'icon', type: 'image/svg+xml', href: '/favicon.svg' },
      ],
      meta: [
        { charset: 'utf-8' },
        { name: 'viewport', content: 'width=device-width, initial-scale=1' },
        { name: 'description', content: 'Autonomous Sentry error triage and fix bot' },
      ],
    },
  },

  // Global CSS
  css: ['~/assets/main.css'],

  // Runtime configuration
  runtimeConfig: {
    // Server-only backend URL for SSR fetches. In Docker Compose the browser
    // reaches the backend at localhost:8000 but the frontend container's own
    // localhost is not the backend — SSR must use the compose service name
    // (set NUXT_API_INTERNAL=http://backend:8000). Empty → fall back to the
    // public base (kube already sets a routable public base for both).
    apiInternal: '',
    public: {
      apiBase: 'http://localhost:8000',
    },
  },

  compatibilityDate: '2025-07-15',

  // TypeScript configuration
  typescript: {
    strict: true,
    typeCheck: false,
    shim: false,
  },

  // ESLint configuration
  eslint: {
    config: {
      stylistic: true,
    },
  },

  // i18n configuration
  i18n: {
    locales: [
      { code: 'en', name: 'English', file: 'en.json' },
    ],
    defaultLocale: 'en',
    langDir: '../app/locales',
    strategy: 'no_prefix',
  },

  // Pinia store configuration
  pinia: {
    storesDirs: ['./app/stores/**'],
  },
})
