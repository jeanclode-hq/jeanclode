import { describe, expect, it } from 'vitest'
import { shortRepoName } from '../app/utils/repo-name'

describe('shortRepoName', () => {
  it('drops the root group from a GitLab path', () => {
    expect(shortRepoName('team-platform/atlas/api')).toBe('atlas/api')
  })

  it('drops the root group from a top-level GitLab project', () => {
    expect(shortRepoName('team-platform/atlas')).toBe('atlas')
  })

  it('leaves namespace-less names alone', () => {
    expect(shortRepoName('jeanclode')).toBe('jeanclode')
  })

  it('returns an empty string unchanged', () => {
    expect(shortRepoName('')).toBe('')
  })
})
