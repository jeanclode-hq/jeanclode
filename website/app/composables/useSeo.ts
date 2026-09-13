const SITE_URL = 'https://jeanclode.com'
const OG_IMAGE = `${SITE_URL}/og-image.jpg`

interface PageSeo {
  title: string
  description: string
  type?: 'website' | 'article'
  publishedAt?: string
}

/** Title, description, canonical and the full og/twitter pair for one page. */
export function usePageSeo({ title, description, type = 'website', publishedAt }: PageSeo) {
  const { path } = useRoute()
  // built from the route rather than the request URL, so a query string or a
  // plain-http origin can never end up in the canonical
  const url = path === '/' ? `${SITE_URL}/` : SITE_URL + path.replace(/\/$/, '')

  useSeoMeta({
    title,
    description,
    ogTitle: title,
    ogDescription: description,
    ogType: type,
    ogUrl: url,
    ogImage: OG_IMAGE,
    twitterCard: 'summary_large_image',
    twitterTitle: title,
    twitterDescription: description,
    twitterImage: OG_IMAGE,
    ...(publishedAt ? { articlePublishedTime: publishedAt } : {}),
  })

  useHead({
    link: [{ rel: 'canonical', href: url }],
  })

  return { url }
}

export function useJsonLd(node: Record<string, unknown>) {
  useHead({
    script: [
      {
        type: 'application/ld+json',
        innerHTML: JSON.stringify({ '@context': 'https://schema.org', ...node }),
      },
    ],
  })
}
