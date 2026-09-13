/**
 * devalue (Nuxt's SSR payload serializer) can't stringify Error instances —
 * they're not plain objects. Any query error that survives to the payload
 * (e.g. a transient backend fetch failure) crashes the whole page render
 * instead of just that query. Teach it how, on both server and client.
 */
export default definePayloadPlugin(() => {
  definePayloadReducer('Error', (data) => data instanceof Error && [data.name, data.message])
  definePayloadReviver('Error', ([name, message]: [string, string]) => {
    const error = new Error(message)
    error.name = name
    return error
  })
})
