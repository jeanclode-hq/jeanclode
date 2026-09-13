export default defineEventHandler((event) => {
  const provider = getRouterParam(event, 'provider')
  const config = useRuntimeConfig()
  const apiBase = config.public.apiBase

  return sendRedirect(event, `${apiBase}/auth/${provider}/authorize`)
})
