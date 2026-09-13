<script setup lang="ts">
const route = useRoute()
const layoutName = computed(() => route.meta.layout as string || 'default')

// site-wide identity: lets search engines and LLMs attribute brand queries
useJsonLd({
  '@graph': [
    {
      '@type': 'Organization',
      '@id': 'https://jeanclode.com/#organization',
      name: 'JeanClode',
      url: 'https://jeanclode.com',
      logo: 'https://jeanclode.com/logo.svg',
      description: 'Open-source, self-hosted autonomous coding agent that turns Sentry errors and Git issues into CI-verified pull requests.',
      sameAs: ['https://github.com/jeanclode-hq'],
    },
    {
      '@type': 'WebSite',
      '@id': 'https://jeanclode.com/#website',
      name: 'JeanClode',
      url: 'https://jeanclode.com',
      publisher: { '@id': 'https://jeanclode.com/#organization' },
      inLanguage: 'en',
    },
  ],
})
</script>

<template>
  <UApp>
    <NuxtLayout :key="layoutName">
      <NuxtPage />
    </NuxtLayout>
  </UApp>
</template>
