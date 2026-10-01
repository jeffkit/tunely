/**
 * App 路由测试（task-5：默认路由会话探测）
 * 覆盖：默认路由未登录 → #/login、已登录 → #/tunnels、
 * legacy 路由可达且未配置 key 时不自动请求、配置 key 后恢复轮询
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import App from './App'
import { consoleApi } from './api/console'
import { api } from './api/client'
import { useSessionStore } from './store/sessionStore'
import { BACKEND_CONFIG_CHANGED_EVENT } from './constants'
import { ConsoleApiError } from './types/errors'

vi.mock('./api/console', () => ({
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
    adminUpdateUser: vi.fn(),
    adminCreateInvite: vi.fn(),
  },
}))

vi.mock('./api/client', () => ({
  api: {
    getServerInfo: vi.fn(),
    listTunnels: vi.fn(),
    getTunnel: vi.fn(),
    createTunnel: vi.fn(),
    updateTunnel: vi.fn(),
    deleteTunnel: vi.fn(),
    regenerateToken: vi.fn(),
    checkAvailability: vi.fn(),
    getTunnelLogs: vi.fn(),
  },
  refreshClient: vi.fn(),
  updateApiBaseUrl: vi.fn(),
  setApiKey: vi.fn(),
}))

const mockConsoleApi = vi.mocked(consoleApi)
const mockLegacyApi = vi.mocked(api)

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

describe('App 默认路由会话探测', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.location.hash = ''
    window.localStorage.clear()
    useSessionStore.setState({ me: null, status: 'unknown', loading: false })
    mockConsoleApi.listMyTunnels.mockResolvedValue([])
    // 旧版管理台（#/legacy）用的 admin key API：给最小合法返回，避免组件挂载即崩
    mockLegacyApi.listTunnels.mockResolvedValue([])
    mockLegacyApi.getServerInfo.mockResolvedValue({
      name: 'tunely-test',
      version: '0.0.0-test',
      domain: { pattern: '', customizable: '', suffix: 'test.local' },
      websocket: { url: 'ws://localhost:8000/ws/tunnel' },
      protocols: [],
    })
  })

  it('默认路由未登录：探测 /me 失败后重定向 #/login', async () => {
    mockConsoleApi.me.mockRejectedValue(new ConsoleApiError('未登录或会话已过期', 401))

    render(<App />)

    await waitFor(() => {
      expect(window.location.hash).toBe('#/login')
    })
    expect(mockConsoleApi.me).toHaveBeenCalledTimes(1)
    // 落到登录页
    expect(await screen.findByText('Tunely 控制台')).toBeInTheDocument()
  })

  it('默认路由已登录：探测 /me 成功后重定向 #/tunnels', async () => {
    mockConsoleApi.me.mockResolvedValue({ username: 'alice', role: 'tenant' })

    render(<App />)

    await waitFor(() => {
      expect(window.location.hash).toBe('#/tunnels')
    })
    // 落到租户主页
    expect(await screen.findByText('新建隧道')).toBeInTheDocument()
    expect(screen.getByText('alice')).toBeInTheDocument()
  })

  it('legacy 路由可达：未配置 admin key 时不渲染仪表盘、不自动请求', async () => {
    window.location.hash = '#/legacy'

    render(<App />)

    // 展示配置提示而非仪表盘
    expect(await screen.findByText('旧版管理台需要 admin key')).toBeInTheDocument()
    expect(screen.queryByText('仪表盘')).not.toBeInTheDocument()

    // 观察一段时间，确认没有自动发起任何旧版 API 请求（消除 401 噪音）
    await sleep(200)
    expect(mockLegacyApi.listTunnels).not.toHaveBeenCalled()
    expect(mockLegacyApi.getServerInfo).not.toHaveBeenCalled()
  })

  it('legacy 路由：已配置 admin key 时正常渲染仪表盘并轮询', async () => {
    window.localStorage.setItem('tunely_api_key', 'legacy-key')
    window.location.hash = '#/legacy'

    render(<App />)

    expect((await screen.findAllByText('仪表盘')).length).toBeGreaterThan(0)
    await waitFor(() => {
      expect(mockLegacyApi.listTunnels).toHaveBeenCalled()
      expect(mockLegacyApi.getServerInfo).toHaveBeenCalled()
    })
  })

  it('legacy 路由：运行中保存配置（事件广播）后恢复自动请求', async () => {
    window.location.hash = '#/legacy'

    render(<App />)
    expect(await screen.findByText('旧版管理台需要 admin key')).toBeInTheDocument()
    expect(mockLegacyApi.listTunnels).not.toHaveBeenCalled()

    // 模拟 BackendConfigManager 保存配置后的事件广播
    window.localStorage.setItem('tunely_api_key', 'legacy-key')
    window.dispatchEvent(new Event(BACKEND_CONFIG_CHANGED_EVENT))

    await waitFor(() => {
      expect(mockLegacyApi.listTunnels).toHaveBeenCalled()
    })
  })

  it('登录页不再出现 legacy 入口（#/legacy 仅保留显式地址访问）', async () => {
    mockConsoleApi.me.mockRejectedValue(new ConsoleApiError('未登录或会话已过期', 401))

    render(<App />)

    // 落到登录页后，不应有任何指向 legacy 的引导入口
    await screen.findByText('没有账号？使用邀请码注册')
    expect(screen.queryByText('admin key 管理台（旧版）')).not.toBeInTheDocument()
    expect(document.body.querySelector('a[href="#/legacy"]')).toBeNull()
  })

  it('#/legacy 路由本身保留：直接输入地址仍可访问', async () => {
    window.location.hash = '#/legacy'

    render(<App />)

    expect(await screen.findByText('旧版管理台需要 admin key')).toBeInTheDocument()
  })
})
