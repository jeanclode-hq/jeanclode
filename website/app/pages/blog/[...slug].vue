<script setup lang="ts">
definePageMeta({ layout: 'blog' })

const route = useRoute()

const { data: post } = await useAsyncData(`blog-${route.path}`, () =>
  queryCollection('blog').path(route.path).first(),
  { watch: [() => route.path] },
)

if (!post.value) {
  throw createError({ statusCode: 404, message: 'Post not found' })
}

const { url } = usePageSeo({
  title: post.value.seoTitle ?? `${post.value.title} - JeanClode`,
  description: post.value.seoDescription ?? post.value.description,
  type: 'article',
  publishedAt: post.value.publishedAt,
})

useJsonLd({
  '@graph': [
    {
      '@type': 'BlogPosting',
      headline: post.value.title,
      description: post.value.description,
      datePublished: post.value.publishedAt,
      mainEntityOfPage: url,
      author: { '@id': 'https://jeanclode.com/#organization' },
      image: 'https://jeanclode.com/og-image.jpg',
      publisher: { '@id': 'https://jeanclode.com/#organization' },
    },
    {
      '@type': 'BreadcrumbList',
      itemListElement: [
        { '@type': 'ListItem', position: 1, name: 'Blog', item: 'https://jeanclode.com/blog' },
        { '@type': 'ListItem', position: 2, name: post.value.title, item: url },
      ],
    },
  ],
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
    <div class="mx-auto max-w-2xl px-6 py-16 md:py-24">
      <div class="mb-4">
        <NuxtLink
          to="/blog"
          class="inline-flex items-center gap-1.5 text-sm text-[var(--ui-text-muted)] hover:text-[var(--ui-text-highlighted)] transition-colors"
        >
          <UIcon name="i-lucide-arrow-left" class="size-4" />
          All posts
        </NuxtLink>
      </div>

      <header class="mb-12 pt-6 border-b border-[var(--ui-border)] pb-10">
        <div class="flex items-center gap-3 mb-6">
          <time class="text-sm text-[var(--ui-text-muted)]">{{ formatDate(post!.publishedAt) }}</time>
          <span v-if="post!.readingTime" class="text-sm text-[var(--ui-text-dimmed)]">· {{ post!.readingTime }} min read</span>
        </div>
        <h1 class="text-3xl font-semibold tracking-[-0.025em] text-[var(--ui-text-highlighted)] md:text-4xl leading-tight mb-4">
          {{ post!.title }}
        </h1>
        <p class="text-lg text-[var(--ui-text-muted)] leading-relaxed">
          {{ post!.description }}
        </p>
      </header>

      <div class="prose prose-neutral max-w-none
        prose-headings:font-semibold prose-headings:tracking-[-0.015em] prose-headings:text-[var(--ui-text-highlighted)]
        prose-h2:text-2xl prose-h2:mt-10 prose-h2:mb-4
        prose-h3:text-lg prose-h3:mt-8 prose-h3:mb-3
        prose-p:text-[var(--ui-text)] prose-p:leading-relaxed prose-p:mb-5
        prose-a:text-[var(--ui-text-highlighted)] prose-a:underline prose-a:underline-offset-2
        prose-code:font-mono prose-code:text-sm prose-code:bg-[var(--ui-bg-elevated)] prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded
        prose-pre:bg-[var(--ui-bg-inverted)] prose-pre:text-[var(--ui-text-inverted)]
        prose-strong:text-[var(--ui-text-highlighted)] prose-strong:font-semibold
        prose-ul:my-5 prose-li:my-1
        prose-blockquote:border-l-2 prose-blockquote:border-[var(--ui-border-accented)] prose-blockquote:pl-4 prose-blockquote:text-[var(--ui-text-muted)] prose-blockquote:italic
      ">
        <ContentRenderer v-if="post" :value="post" />
      </div>

      <footer class="mt-16 pt-8 border-t border-[var(--ui-border)]">
        <NuxtLink
          to="/blog"
          class="inline-flex items-center gap-1.5 text-sm font-medium text-[var(--ui-text-highlighted)] hover:gap-2.5 transition-all"
        >
          <UIcon name="i-lucide-arrow-left" class="size-4" />
          Back to all posts
        </NuxtLink>
      </footer>
    </div>
  </main>
</template>
