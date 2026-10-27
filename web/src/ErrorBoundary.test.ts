import { describe, expect, it } from 'vitest'
import { ErrorBoundary } from './ErrorBoundary'

const boom = new Error('boom')

describe('ErrorBoundary.getDerivedStateFromProps', () => {
  it('remembers the page while nothing is wrong', () => {
    expect(ErrorBoundary.getDerivedStateFromProps({ resetKey: '/a' }, { error: null, errorKey: undefined })).toEqual({
      errorKey: '/a',
    })
  })

  it('keeps showing the error on the page where it happened', () => {
    expect(ErrorBoundary.getDerivedStateFromProps({ resetKey: '/a' }, { error: boom, errorKey: '/a' })).toBeNull()
  })

  it('clears the error once the page changes', () => {
    expect(ErrorBoundary.getDerivedStateFromProps({ resetKey: '/b' }, { error: boom, errorKey: '/a' })).toEqual({
      error: null,
      errorKey: '/b',
    })
  })
})
