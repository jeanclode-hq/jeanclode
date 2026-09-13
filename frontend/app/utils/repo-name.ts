/** GitLab repositories are stored under their full `path_with_namespace`
 *  (`root-group/subgroup/project`). The leading segment is the connected org
 *  itself — it is already implied by the surrounding org context (source pills,
 *  the picker's org step, the Sentry org page) and repeating it on every row is
 *  what pushes names past the truncation point. Drop it and keep the subgroup
 *  path. GitHub names carry no namespace and pass through untouched. */
export function shortRepoName(name: string): string {
  const slash = name.indexOf('/')
  return slash === -1 ? name : name.slice(slash + 1)
}
