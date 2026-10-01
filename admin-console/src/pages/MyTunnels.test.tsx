/**
 * 租户主页关键交互测试
 * 覆盖：新建隧道（token 一次性展示闭环）、删除二次确认、轮换 Token、配额 409、未登录跳转
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MyTunnels } from './MyTunnels'
import { consoleApi } from '../api/console'
import { useSessionStore } from '../store/sessionStore'
import { ConsoleApiError } from '../types/errors'
import type { TenantTunnel } from '../types/console'

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

const tenantTunnel: TenantTunnel = {
  domain: 'demo.example.com',
  created_at: '2026-01-01T00:00:00Z',
  online: true,
  last_seen_at: '2026-01-01T12:00:00Z',
  bytes_in: 1536,
  bytes_out: 2048,
}

async function renderAuthedTunnels() {
  mockApi.me.mockResolvedValue({ username: 'alice', role: 'tenant' })
  render(<MyTunnels />)
  // 等待列表加载完成
  await screen.findByText('demo.example.com')
}

describe('MyTunnels', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.location.hash = ''
    useSessionStore.setState({ me: null, status: 'unknown', loading: false })
    mockApi.listMyTunnels.mockResolvedValue([tenantTunnel])
  })

  it('加载会话与隧道列表，展示在线徽标与流量', async () => {
    await renderAuthedTunnels()

    expect(mockApi.me).toHaveBeenCalled()
    expect(mockApi.listMyTunnels).toHaveBeenCalled()
    expect(screen.getByText('alice')).toBeInTheDocument()
    expect(screen.getByText('在线')).toBeInTheDocument()
    // 入 1.50 KB / 出 2.00 KB
    expect(screen.getByText(/1\.50 KB/)).toBeInTheDocument()
    expect(screen.getByText(/2\.00 KB/)).toBeInTheDocument()
  })

  it('新建隧道：前缀 → 创建成功弹层一次性展示域名 + token，确认后明文消失', async () => {
    const user = userEvent.setup()
    mockApi.createMyTunnel.mockResolvedValue({ domain: 'demo.example.com', token: 'tun_secret_1' })

    await renderAuthedTunnels()

    await user.type(screen.getByPlaceholderText(/隧道前缀/), 'demo')
    await user.click(screen.getByRole('button', { name: '创建隧道' }))

    await waitFor(() => {
      expect(mockApi.createMyTunnel).toHaveBeenCalledWith('demo')
    })

    // 弹层一次性展示域名 + token
    expect(await screen.findByText('隧道创建成功')).toBeInTheDocument()
    expect(screen.getByText('tun_secret_1')).toBeInTheDocument()

    // 「我已保存」→ 清除状态，token 不再出现
    await user.click(screen.getByRole('button', { name: '我已保存' }))
    await waitFor(() => {
      expect(screen.queryByText('tun_secret_1')).not.toBeInTheDocument()
    })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('新建隧道配额 409（quota_exceeded）：提示且不弹 token 弹层', async () => {
    const user = userEvent.setup()
    mockApi.createMyTunnel.mockRejectedValue(
      new ConsoleApiError('资源冲突: quota exceeded', 409, 'quota_exceeded')
    )

    await renderAuthedTunnels()

    await user.type(screen.getByPlaceholderText(/隧道前缀/), 'demo')
    await user.click(screen.getByRole('button', { name: '创建隧道' }))

    await screen.findByText(/已达配额上限/)
    expect(screen.queryByText(/Token 明文/)).not.toBeInTheDocument()
  })

  it('删除隧道：Popconfirm 二次确认后才调用删除接口', async () => {
    const user = userEvent.setup()
    // 删除后列表刷新为空
    mockApi.listMyTunnels
      .mockResolvedValueOnce([tenantTunnel])
      .mockResolvedValueOnce([])
    mockApi.deleteMyTunnel.mockResolvedValue(undefined)

    await renderAuthedTunnels()

    await user.click(screen.getByRole('button', { name: /^删\s*除$/ }))

    // 二次确认弹层出现，此时还未调用接口
    expect(screen.getByText('确认删除该隧道？')).toBeInTheDocument()
    expect(mockApi.deleteMyTunnel).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: '确认删除' }))

    await waitFor(() => {
      expect(mockApi.deleteMyTunnel).toHaveBeenCalledWith('demo.example.com')
    })
    // 列表刷新
    await screen.findByText(/暂无隧道/)
  })

  it('删除隧道：取消二次确认则不调用删除接口', async () => {
    const user = userEvent.setup()

    await renderAuthedTunnels()

    await user.click(screen.getByRole('button', { name: /^删\s*除$/ }))
    await user.click(screen.getByRole('button', { name: /^取\s*消$/ }))

    expect(mockApi.deleteMyTunnel).not.toHaveBeenCalled()
    expect(screen.getByText('demo.example.com')).toBeInTheDocument()
  })

  it('轮换 Token：二次确认后展示新 token，确认后消失', async () => {
    const user = userEvent.setup()
    mockApi.rotateTunnelToken.mockResolvedValue({
      domain: 'demo.example.com',
      token: 'tun_rotated_new',
    })

    await renderAuthedTunnels()

    await user.click(screen.getByRole('button', { name: '轮换 Token' }))
    expect(screen.getByText(/旧 Token 将立即失效/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '确认轮换' }))

    expect(await screen.findByText('Token 已轮换')).toBeInTheDocument()
    expect(screen.getByText('tun_rotated_new')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '我已保存' }))
    await waitFor(() => {
      expect(screen.queryByText('tun_rotated_new')).not.toBeInTheDocument()
    })
  })

  it('未登录（me 401）跳转登录页', async () => {
    mockApi.me.mockRejectedValue(new ConsoleApiError('未登录或会话已过期', 401))

    render(<MyTunnels />)

    await waitFor(() => {
      expect(window.location.hash).toBe('#/login')
    })
  })

  it('登出：调用 logout 并跳转登录页', async () => {
    const user = userEvent.setup()
    mockApi.logout.mockResolvedValue(undefined)

    await renderAuthedTunnels()

    await user.click(screen.getByRole('button', { name: '退出登录' }))

    await waitFor(() => {
      expect(mockApi.logout).toHaveBeenCalled()
      expect(window.location.hash).toBe('#/login')
    })
  })
})
