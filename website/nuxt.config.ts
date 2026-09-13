import { createRequire } from 'module'
import { readdirSync } from 'fs'
import { join } from 'path'

const require = createRequire(import.meta.url)

function getContentUrls(dir: string, prefix: string): string[] {
  const urls: string[] = []
  try {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = join(dir, entry.name)
      if (entry.isDirectory()) {
        urls.push(...getContentUrls(full, `${prefix}/${entry.name.replace(/^\d+\./, '')}`))
      }
      else if (entry.name === 'index.md') {
        urls.push(prefix)
      }
      else if (entry.name.endsWith('.md')) {
        urls.push(`${prefix}/${entry.name.replace(/^\d+\./, '').replace(/\.md$/, '')}`)
      }
    }
  }
  catch {}
  return urls
}

const contentUrls = [
  ...getContentUrls('content/docs', '/docs'),
  ...getContentUrls('content/blog', '/blog'),
]

// Prevent duplicate Vue bundling inside Cloudflare Workers
const vueInternalAliases = Object.fromEntries(
  ['@vue/runtime-core', '@vue/runtime-dom', '@vue/shared', '@vue/reactivity'].map(
    (pkg) => [pkg, require.resolve(`${pkg}/dist/${pkg.split('/').pop()}.esm-bundler.js`)],
  ),
)

export default defineNuxtConfig({
  modules: ['@nuxt/ui', '@nuxtjs/sitemap', '@nuxt/content', '@nuxt/fonts'],

  // self-hosted at build time: no Google Fonts round trips at runtime
  fonts: {
    families: [
      { name: 'Geist', provider: 'google', weights: ['100 900'], styles: ['normal'] },
      { name: 'JetBrains Mono', provider: 'google', weights: [400, 500], styles: ['normal'] },
    ],
    defaults: { subsets: ['latin', 'latin-ext'] },
  },

  content: {
    highlight: {
      theme: {
        default: 'github-light',
        dark: 'github-dark',
      },
    },
    database: {
      type: 'd1',
      bindingName: 'DB',
    },
  },

  vite: {
    server: {
      allowedHosts: ['jeanclode.com', 'www.jeanclode.com', 'moshe-unstridulous-doug.ngrok-free.dev'],
    },
    resolve: {
      alias: vueInternalAliases,
    },
  },

  site: {
    url: 'https://jeanclode.com',
  },

  sitemap: {
    urls: ['/', '/pricing', '/privacy', '/terms', ...contentUrls],
    zeroRuntime: true,
  },

  app: {
    head: {
      htmlAttrs: { lang: 'en' },
      meta: [
        { charset: 'utf-8' },
        { name: 'viewport', content: 'width=device-width, initial-scale=1' },
        { property: 'og:site_name', content: 'JeanClode' },
        { property: 'og:locale', content: 'en_US' },
        { property: 'og:type', content: 'website' },
        { property: 'og:image', content: 'https://jeanclode.com/og-image.jpg' },
        { property: 'og:image:width', content: '1200' },
        { property: 'og:image:height', content: '630' },
      ],
      link: [
        { rel: 'icon', type: 'image/svg+xml', href: '/logo.svg' },
        { rel: 'icon', type: 'image/png', sizes: '32x32', href: '/favicon-32x32.png' },
        { rel: 'icon', type: 'image/png', sizes: '16x16', href: '/favicon-16x16.png' },
        { rel: 'apple-touch-icon', sizes: '180x180', href: '/apple-touch-icon.png' },
        { rel: 'llms', href: '/llms.txt' },
      ],
    },
  },

  css: ['~/assets/main.css'],

  colorMode: {
    preference: 'light',
  },

  nitro: {
    preset: 'cloudflare_module',
    cloudflare: {
      nodeCompat: true,
    },
    prerender: {
      autoSubfolderIndex: false,
      crawlLinks: true,
      routes: ['/', '/pricing', '/docs', '/blog', '/privacy', '/terms'],
      failOnError: false,
    },
    alias: vueInternalAliases,
  },

  compatibilityDate: '2025-07-15',
})
