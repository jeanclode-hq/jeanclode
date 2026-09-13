const CANONICAL_HOST = 'jeanclode.com'

// Cloudflare answers http:// and www. with a 200 and a 521 respectively, so both
// are duplicates of the apex origin. Collapse them onto one canonical URL.
export default defineEventHandler((event) => {
  const host = getRequestHeader(event, 'host')
  if (!host) return

  const bare = host.split(':')[0]!
  if (bare === 'localhost' || bare.endsWith('.localhost') || bare.endsWith('.ngrok-free.dev')) return

  const proto = getRequestHeader(event, 'x-forwarded-proto') ?? 'https'
  if (proto === 'https' && bare === CANONICAL_HOST) return

  return sendRedirect(event, `https://${CANONICAL_HOST}${event.path}`, 301)
})
