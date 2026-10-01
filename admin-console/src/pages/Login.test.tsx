/**
 * 登录页测试：失败提示、成功后建立会话并跳转 /tunnels
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Login } from './Login'
import { consoleApi } from '../api/console'
import { useSessionStore } from '../store/sessionStore'
import { ConsoleApiError } from '../types/errors'

vi.mock('../api/console', () => ({
  consoleApi: {
    me: vi.fn(),
    listMyTunnels: vi.fn(),
    createMyTunnel: vi.fn(),
    rotateTunnelToken: vi.fn(),
    deleteMyTunnel: vi.fn(),
    logout: vi.fn(),
    getEntry: vi.fn(),
    register: vi.fn(),
    login: vi.fn(),
    adminListUsers: vi.fn(),
    adminSetUserDisabled: vi.fn(),
    adminCreateInvite: vi.fn(),
  },
}))

const mockApi = vi.mocked(consoleApi)

describe('Login', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.location.hash = ''
    useSessionStore.setState({ me: null, status: 'unknown', loading: false })
  })

  it('登录成功：建立会话并跳转 /tunnels', async () => {
    const user = userEvent.setup()
    mockApi.login.mockResolvedValue({ username: 'alice', role: 'tenant' })
    mockApi.me.mockResolvedValue({ username: 'alice', role: 'tenant' })

    render(<Login />)

    await user.type(screen.getByLabelText('用户名'), 'alice')
    await user.type(screen.getByLabelText('密码'), 'password-123')
    await user.click(screen.getByRole('button', { name: /登\s*录/ }))

    await waitFor(() => {
      expect(mockApi.login).toHaveBeenCalledWith({ username: 'alice', password: 'password-123' })
      expect(window.location.hash).toBe('#/tunnels')
    })
    expect(useSessionStore.getState().status).toBe('authed')
  })

  it('登录失败（401）：提示错误且不跳转', async () => {
    const user = userEvent.setup()
    mockApi.login.mockRejectedValue(
      new ConsoleApiError('未登录或会话已过期: 用户名或密码错误', 401)
    )

    render(<Login />)

    await user.type(screen.getByLabelText('用户名'), 'alice')
    await user.type(screen.getByLabelText('密码'), 'wrong-password')
    await user.click(screen.getByRole('button', { name: /登\s*录/ }))

    expect(await screen.findByText(/用户名或密码错误/)).toBeInTheDocument()
    expect(window.location.hash).toBe('')
  })

  it('必填校验：用户名/密码为空时不提交', async () => {
    const user = userEvent.setup()
    render(<Login />)

    await user.click(screen.getByRole('button', { name: /登\s*录/ }))

    expect(await screen.findByText('请输入用户名')).toBeInTheDocument()
    expect(await screen.findByText('请输入密码')).toBeInTheDocument()
    expect(mockApi.login).not.toHaveBeenCalled()
  })

  it('提供注册页入口链接', () => {
    render(<Login />)
    expect(screen.getByText('没有账号？使用邀请码注册')).toHaveAttribute('href', '#/register')
  })
})
