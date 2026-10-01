/**
 * 管理页测试：用户列表/禁用（二次确认）、邀请码签发（code 展示 + 复制）、非 admin 拒绝访问
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Admin } from './Admin'
import { consoleApi } from '../api/console'
import { copyTextToClipboard } from '../utils/clipboard'
import { useSessionStore } from '../store/sessionStore'
import { ConsoleApiError } from '../types/errors'
import type { AdminUser } from '../types/console'

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

vi.mock('../utils/clipboard', () => ({
  copyTextToClipboard: vi.fn(),
}))

const mockApi = vi.mocked(consoleApi)
const mockCopy = vi.mocked(copyTextToClipboard)

const users: AdminUser[] = [
  {
    id: 1,
    username: 'alice',
    role: 'tenant',
    disabled: false,
    created_at: '2026-01-01T00:00:00Z',
  },
  {
    id: 2,
    username: 'root',
    role: 'admin',
    disabled: false,
    created_at: '2026-01-01T00:00:00Z',
  },
]

async function renderAsAdmin() {
  mockApi.me.mockResolvedValue({ username: 'root', role: 'admin' })
  mockApi.adminListUsers.mockResolvedValue(users)
  render(<Admin />)
  await screen.findByText('alice')
}

describe('Admin', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.location.hash = ''
    useSessionStore.setState({ me: null, status: 'unknown', loading: false })
    mockCopy.mockResolvedValue(true)
  })

  it('admin 加载用户列表，admin 行不提供禁用操作', async () => {
    await renderAsAdmin()

    expect(screen.getByText('alice')).toBeInTheDocument()
    expect(screen.getAllByText('root').length).toBeGreaterThan(0)

    // alice 行有禁用按钮，root 行没有
    const disableButtons = screen.getAllByRole('button', { name: /^禁\s*用$/ })
    expect(disableButtons).toHaveLength(1)
  })

  it('禁用用户：二次确认后调用接口并刷新列表', async () => {
    const user = userEvent.setup()
    mockApi.adminSetUserDisabled.mockResolvedValue(undefined)
    mockApi.adminListUsers
      .mockResolvedValueOnce(users)
      .mockResolvedValueOnce([{ ...users[0], disabled: true }, users[1]])

    await renderAsAdmin()

    await user.click(screen.getByRole('button', { name: /^禁\s*用$/ }))
    expect(screen.getByText('确认禁用该用户？')).toBeInTheDocument()
    expect(mockApi.adminSetUserDisabled).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: '确认禁用' }))

    await waitFor(() => {
      expect(mockApi.adminSetUserDisabled).toHaveBeenCalledWith({
        username: 'alice',
        disabled: true,
      })
    })
    // 刷新后展示已禁用状态与「启用」操作
    await screen.findByText('已禁用')
    expect(screen.getByRole('button', { name: /^启\s*用$/ })).toBeInTheDocument()
  })

  it('签发邀请码：展示生成的 code 并支持复制，确认后不再展示', async () => {
    const user = userEvent.setup()
    mockApi.adminCreateInvite.mockResolvedValue({ code: 'dsh-abc123', max_uses: 1 })

    await renderAsAdmin()

    await user.click(screen.getByRole('button', { name: /^签\s*发$/ }))

    expect(await screen.findByText('邀请码签发成功')).toBeInTheDocument()
    expect(screen.getByText('dsh-abc123')).toBeInTheDocument()
    expect(mockApi.adminCreateInvite).toHaveBeenCalledWith({
      max_uses: undefined,
      expires_days: undefined,
    })

    await user.click(screen.getByRole('button', { name: /复\s*制/ }))
    expect(mockCopy).toHaveBeenCalledWith('dsh-abc123')

    await user.click(screen.getByRole('button', { name: '我已保存' }))
    await waitFor(() => {
      expect(screen.queryByText('dsh-abc123')).not.toBeInTheDocument()
    })
  })

  it('非 admin 角色：显示 403 拒绝页，不调用管理端接口', async () => {
    mockApi.me.mockResolvedValue({ username: 'alice', role: 'tenant' })

    render(<Admin />)

    expect(await screen.findByText('仅管理员可访问')).toBeInTheDocument()
    expect(mockApi.adminListUsers).not.toHaveBeenCalled()
  })

  it('管理端 401（未配置 admin key）：提示需要 admin key', async () => {
    mockApi.me.mockResolvedValue({ username: 'root', role: 'admin' })
    mockApi.adminListUsers.mockRejectedValue(new ConsoleApiError('未登录或会话已过期', 401))

    render(<Admin />)

    expect(await screen.findByText(/需要 admin key/)).toBeInTheDocument()
  })
})
