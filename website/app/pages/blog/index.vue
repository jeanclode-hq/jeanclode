<script setup lang="ts">
definePageMeta({ layout: 'blog' })

const { data: posts } = await useAsyncData('blog-index', () =>
  queryCollection('blog').order('publishedAt', 'DESC').all(),
)

usePageSeo({
  title: 'Blog — Autonomous Coding Agents | JeanClode',
  description: 'Technical articles on autonomous coding agents, Sentry error resolution, AI code review pipelines, and self-hosted developer tooling that ships its own PRs.',
})

useJsonLd({
  '@type': 'Blog',
  name: 'JeanClode Blog',
  url: 'https://jeanclode.com/blog',
  publisher: { '@id': 'https://jeanclode.com/#organization' },
  blogPost: (posts.value ?? []).map(post => ({
    '@type': 'BlogPosting',
    headline: post.title,
    description: post.description,
    datePublished: post.publishedAt,
    url: `https://jeanclode.com${post.path}`,
  })),
})

function formatDate(dateStr: string) {
  return new Date(dateStr).toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  })
}
</script>

<template>
  <main>
    <div class="mx-auto max-w-3xl px-6 py-16 md:py-24">
      <div class="mb-12">
        <span class="inline-block rounded-full border border-[var(--ui-border)] px-4 py-1.5 text-xs font-semibold uppercase tracking-widest text-[var(--ui-text-muted)] mb-6">
          Engineering
        </span>
        <h1 class="text-4xl font-semibold tracking-[-0.02em] text-[var(--ui-text-highlighted)] md:text-5xl">
          Blog
        </h1>
        <p class="mt-4 text-lg text-[var(--ui-text-muted)]">
          Deep dives on autonomous agents, Sentry error resolution, and self-hosted AI tooling.
        </p>
      </div>

      <div class="divide-y divide-[var(--ui-border)]">
        <article
          v-for="post in posts"
          :key="post.path"
          class="py-8 group"
        >
          <NuxtLink :to="post.path" class="block">
            <div class="flex items-center gap-3 mb-3">
              <time class="text-sm text-[var(--ui-text-muted)]">{{ formatDate(post.publishedAt) }}</time>
              <span v-if="post.readingTime" class="text-sm text-[var(--ui-text-dimmed)]">· {{ post.readingTime }} min read</span>
            </div>
            <h2 class="text-xl font-semibold tracking-[-0.015em] text-[var(--ui-text-highlighted)] group-hover:text-[var(--ui-primary)] transition-colors mb-2">
              {{ post.title }}
            </h2>
            <p class="text-[var(--ui-text-muted)] leading-relaxed line-clamp-2">
              {{ post.description }}
            </p>
            <div class="mt-4 flex items-center gap-1 text-sm font-medium text-[var(--ui-text-highlighted)] group-hover:gap-2 transition-all">
              Read more
              <UIcon name="i-lucide-arrow-right" class="size-4" />
            </div>
          </NuxtLink>
        </article>
      </div>

      <div v-if="!posts?.length" class="py-16 text-center text-[var(--ui-text-muted)]">
        No posts yet.
      </div>
    </div>
  </main>
</template>
