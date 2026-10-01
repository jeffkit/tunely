/**
 * 哈希路由测试
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { getHashPath, navigate, useHashRoute } from './router'

function RouteProbe() {
  const path = useHashRoute()
  return <span data-testid="route">{path}</span>
}

describe('hash router', () => {
  beforeEach(() => {
    window.location.hash = ''
  })

  afterEach(() => {
    window.location.hash = ''
  })

  it('无哈希时路径为 /', () => {
    expect(getHashPath()).toBe('/')
  })

  it('navigate 修改哈希路径', () => {
    navigate('/login')
    expect(window.location.hash).toBe('#/login')
    expect(getHashPath()).toBe('/login')
  })

  it('useHashRoute 跟随哈希变化重渲染', async () => {
    render(<RouteProbe />)
    expect(screen.getByTestId('route').textContent).toBe('/')

    // hashchange 事件与 React 重渲染都是异步的
    navigate('/tunnels')
    await waitFor(() => {
      expect(screen.getByTestId('route').textContent).toBe('/tunnels')
    })

    navigate('/admin')
    await waitFor(() => {
      expect(screen.getByTestId('route').textContent).toBe('/admin')
    })
  })

  it('navigate replace 触发一次 hashchange 并替换路径', () => {
    const handler = vi.fn()
    window.addEventListener('hashchange', handler)

    navigate('/login', { replace: true })

    expect(getHashPath()).toBe('/login')
    expect(handler).toHaveBeenCalledTimes(1)

    window.removeEventListener('hashchange', handler)
  })
})
