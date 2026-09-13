<script setup lang="ts">
definePageMeta({ layout: 'docs' })

const route = useRoute()

const { data: navigation } = useNuxtData('docs-navigation')

const docsNavigation = computed(() => {
  if (!navigation.value) return []
  if (navigation.value.length === 1 && navigation.value[0]?.children) {
    return navigation.value[0].children
  }
  return navigation.value
})

const { data: page } = await useAsyncData(`docs-${route.path}`, () =>
  queryCollection('docs').path(route.path).first(),
  { watch: [() => route.path] },
)

if (!page.value) {
  throw createError({ statusCode: 404, message: 'Page not found' })
}

const { data: surround } = await useAsyncData(`docs-${route.path}-surround`, () =>
  queryCollectionItemSurroundings('docs', route.path),
  { watch: [() => route.path] },
)

const { url } = usePageSeo({
  title: page.value.seoTitle ?? `${page.value.title} - JeanClode Docs`,
  description: page.value.seoDescription ?? page.value.description,
  type: 'article',
})

const segments = route.path.split('/').filter(Boolean)

useJsonLd({
  '@graph': [
    {
      '@type': 'TechArticle',
      headline: page.value.title,
      description: page.value.description,
      mainEntityOfPage: url,
      publisher: { '@id': 'https://jeanclode.com/#organization' },
    },
    {
      '@type': 'BreadcrumbList',
      // section segments like /docs/workflows have no page of their own, so they
      // carry a name and no item rather than a link to a 404
      itemListElement: segments.map((segment, i) => {
        const last = i === segments.length - 1
        const href = `https://jeanclode.com/${segments.slice(0, i + 1).join('/')}`
        return {
          '@type': 'ListItem',
          position: i + 1,
          name: last
            ? page.value!.title
            : segment.replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase()),
          ...(last || i === 0 ? { item: href } : {}),
        }
      }),
    },
  ],
})
</script>

<template>
  <UContainer>
    <UPage>
      <template #left>
        <UPageAside>
          <UContentNavigation :navigation="docsNavigation" :default-open="true" highlight />
        </UPageAside>
      </template>

      <UPageBody>
        <UPageHeader :title="page!.title" :description="page!.description" />

        <div class="prose prose-neutral max-w-none">
          <ContentRenderer v-if="page" :value="page" />
        </div>

        <UContentSurround :surround="surround!" />
      </UPageBody>

      <template #right>
        <UPageAside>
          <UContentToc :links="page!.body?.toc?.links" highlight />
        </UPageAside>
      </template>
    </UPage>
  </UContainer>
</template>
