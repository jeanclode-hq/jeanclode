<script setup lang="ts">
usePageSeo({
  title: 'Terms of Service | JeanClode',
  description: 'Terms governing use of jeanclode.com and of the JeanClode open-source software, which is distributed under the GNU Affero General Public License v3.',
})
</script>

<template>
  <UContainer>
    <UPage>
      <UPageBody>
        <UPageHeader title="Terms of Service" description="Last updated August 25, 2026" />

        <div class="legal-content mx-auto max-w-3xl">
          <h2>Agreement to terms</h2>
          <p>
            By accessing or using <strong>jeanclode.com</strong> (the "Website") you agree to these Terms.
            If you don't agree, please don't use the Website.
          </p>

          <h2>The software</h2>
          <p>
            JeanClode is free, open-source software released under the
            <a href="https://github.com/jeanclode-hq/jeanclode/blob/main/LICENSE" target="_blank" rel="noopener noreferrer">GNU Affero General Public License v3.0</a>.
            Your use of the software is governed entirely by that license, not by these Terms — these
            Terms only cover your use of the Website itself.
          </p>

          <h2>Use of the website</h2>
          <p>
            The Website is provided for informational purposes: documentation, a blog, and links to the
            open-source project. You agree not to misuse it — for example, by attempting unauthorized
            access, disrupting its availability, or scraping it at a rate that degrades service for others.
          </p>

          <h2>No warranty</h2>
          <p>
            The Website and the JeanClode software are provided "as is," without warranty of any kind,
            express or implied, to the fullest extent permitted by applicable law. JeanClode autonomously
            proposes code changes and opens pull requests against repositories you connect it to — you are
            solely responsible for reviewing every change it produces before merging it. We don't guarantee
            the software will be error-free, secure, or fit for any particular purpose.
          </p>

          <h2>Limitation of liability</h2>
          <p>
            To the maximum extent permitted by applicable law, JeanClode and its contributors will not be
            liable for any indirect, incidental, or consequential damages — including but not limited to
            unintended code changes, service interruptions, or data loss — arising from your use of the
            Website or the software.
          </p>

          <h2>Your responsibility when self-hosting</h2>
          <p>
            When you deploy JeanClode yourself, you are solely responsible for: the infrastructure you run
            it on; any credentials, API keys, or tokens you provide it (GitHub, GitLab, Sentry, LLM
            providers, etc.); complying with the terms of those third-party services; and reviewing any
            pull requests it opens before merging them.
          </p>

          <h2>Intellectual property</h2>
          <p>
            The JeanClode name and logo identify the project. The source code is licensed under AGPL-3.0 —
            see the <a href="https://github.com/jeanclode-hq/jeanclode/blob/main/LICENSE" target="_blank" rel="noopener noreferrer">LICENSE</a>
            file for the exact terms. Website content such as documentation and blog posts is © JeanClode
            Contributors unless otherwise noted.
          </p>

          <h2>Third-party services</h2>
          <p>
            The Website may link to third-party services, including GitHub, GitLab, Sentry, and Anthropic.
            We don't control and aren't responsible for the content, availability, or practices of those
            third parties.
          </p>

          <h2>Changes to these terms</h2>
          <p>
            We may revise these Terms from time to time. Continued use of the Website after a change is
            posted constitutes acceptance of the revised Terms.
          </p>

          <h2>Contact</h2>
          <p>
            Questions about these Terms: <a href="mailto:adam.saimi@jeanclode.com">adam.saimi@jeanclode.com</a>.
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
