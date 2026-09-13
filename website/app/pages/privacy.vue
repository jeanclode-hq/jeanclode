<script setup lang="ts">
usePageSeo({
  title: 'Privacy Policy | JeanClode',
  description: 'How JeanClode collects, uses and stores information on jeanclode.com, and what the self-hosted software does with the code it runs against.',
})
</script>

<template>
  <UContainer>
    <UPage>
      <UPageBody>
        <UPageHeader title="Privacy Policy" description="Last updated August 25, 2026" />

        <div class="legal-content mx-auto max-w-3xl">
          <p>
            This policy covers <strong>jeanclode.com</strong> (the "Website") only. It does not cover
            your own self-hosted deployment of the JeanClode software — see
            <a href="#self-hosted-software">Self-Hosted Software</a> below for how that's handled.
          </p>

          <h2>Information we collect</h2>
          <p>
            This Website is a static site: documentation, a blog, and links to the open-source project.
            You don't need an account or to provide any personal information to browse it, and we don't
            currently run analytics, advertising trackers, or a newsletter signup.
          </p>
          <p>
            Our hosting provider (Cloudflare Pages) automatically logs standard technical request data —
            IP address, browser/user agent, pages requested, and timestamps — for security, abuse
            prevention, and performance purposes. This is Cloudflare's infrastructure logging, not
            something JeanClode configures or has separate access to; see
            <a href="https://www.cloudflare.com/privacypolicy/" target="_blank" rel="noopener noreferrer">Cloudflare's privacy policy</a>
            for details.
          </p>

          <h2>Cookies</h2>
          <p>
            We don't set first-party cookies for tracking, advertising, or analytics. Cloudflare may set
            essential cookies required to serve the site and protect it from abuse.
          </p>

          <h2 id="self-hosted-software">Self-hosted software</h2>
          <p>
            JeanClode itself is open-source software, licensed under AGPL-3.0, that you download and run
            on your own infrastructure. When you self-host JeanClode, everything it touches — your source
            code, Sentry error data, GitHub/GitLab tokens, and any LLM API calls it makes — stays within
            the infrastructure and accounts you configure. None of it is transmitted to us, and we have no
            visibility into or access to your deployment or its data.
          </p>

          <h2>Third-party links</h2>
          <p>
            This Website links to third-party services and destinations — GitHub, integration docs for
            services like Sentry, and the Anthropic/Claude API among others. We aren't responsible for the
            privacy practices of those third parties; review their own policies before using them.
          </p>

          <h2>Children's privacy</h2>
          <p>
            This Website is not directed at children under 13, and we don't knowingly collect information
            from them.
          </p>

          <h2>Changes to this policy</h2>
          <p>
            We may update this policy from time to time. Changes will be posted on this page with a
            revised "last updated" date above.
          </p>

          <h2>Contact</h2>
          <p>
            Questions about this policy: <a href="mailto:adam.saimi@jeanclode.com">adam.saimi@jeanclode.com</a>.
            Security vulnerabilities should be reported to
            <a href="mailto:adam.saimi@jeanclode.com">adam.saimi@jeanclode.com</a> per our
            <a href="https://github.com/jeanclode-hq/jeanclode/blob/main/SECURITY.md" target="_blank" rel="noopener noreferrer">security policy</a>.
          </p>
        </div>
      </UPageBody>
    </UPage>
  </UContainer>
</template>

<style scoped>
.legal-content h2 {
  margin-top: 2.5rem;
  margin-bottom: 0.75rem;
  font-size: 1.25rem;
  font-weight: 600;
  letter-spacing: -0.01em;
  color: var(--ui-text-highlighted);
}
.legal-content h2:first-child {
  margin-top: 0;
}
.legal-content p {
  margin-bottom: 1rem;
  font-size: 1rem;
  line-height: 1.75rem;
  color: var(--ui-text-muted);
}
.legal-content a {
  color: var(--ui-text-highlighted);
  text-decoration: underline;
  text-underline-offset: 2px;
}
.legal-content a:hover {
  color: var(--ui-text);
}
.legal-content strong {
  font-weight: 600;
  color: var(--ui-text-highlighted);
}
</style>
