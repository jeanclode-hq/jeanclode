import { defineCollection, defineContentConfig, z } from '@nuxt/content'
import { asSitemapCollection } from '@nuxtjs/sitemap/content'

export default defineContentConfig({
  collections: {
    docs: defineCollection(asSitemapCollection({
      type: 'page',
      source: 'docs/**',
      schema: z.object({
        title: z.string(),
        description: z.string(),
        icon: z.string().optional(),
        // SERP copy, when the on-page title/description are the wrong length for it
        seoTitle: z.string().optional(),
        seoDescription: z.string().optional(),
      }),
    })),
    blog: defineCollection(asSitemapCollection({
      type: 'page',
      source: 'blog/**',
      schema: z.object({
        title: z.string(),
        description: z.string(),
        publishedAt: z.string(),
        readingTime: z.number().optional(),
        seoTitle: z.string().optional(),
        seoDescription: z.string().optional(),
      }),
    })),
  },
})
