/**
 * 管理页测试（契约 v1.1：纯会话鉴权）
 * 覆盖：用户列表/禁用（二次确认）、self_lockout 专门提示、行内角色调整、
 * 邀请码角色选择（admin 需二次确认）、非 admin 403、401 跳登录页
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
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
    adminUpdateUser: vi.fn(),
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

async function renderAsAdmin(list: AdminUser[] = users) {
  mockApi.me.mockResolvedValue({ username: 'root', role: 'admin' })
  mockApi.adminListUsers.mockResolvedValue(list)
  render(<Admin />)
  await screen.findByText('alice')
}

/** 点击 antd 单选按钮（原生 input 被 pointer-events:none 隐藏，需点其包装 label） */
async function clickRadio(user: ReturnType<typeof userEvent.setup>, name: string) {
  const radio = screen.getByRole('radio', { name })
  const wrapper = radio.closest('label')
  if (!wrapper) throw new Error(`radio label not found: ${name}`)
  await user.click(wrapper)
}

/** 定位某一行内的操作按钮（Popconfirm 弹层渲染在 body 门户，不会混入行内查询） */
function rowOf(username: string): HTMLElement {
  // 用户名同时出现在顶栏与会话头部，取位于表格行内的那个
  const row = screen
    .getAllByText(username)
    .map((el) => el.closest('tr'))
    .find((tr): tr is HTMLTableRowElement => tr !== null)
  if (!row) throw new Error(`table row not found: ${username}`)
  return row
}

describe('Admin', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.location.hash = ''
    useSessionStore.setState({ me: null, status: 'unknown', loading: false })
    mockCopy.mockResolvedValue(true)
  })

  it('admin 加载用户列表：role 列展示，每行有禁用/启用与角色调整操作', async () => {
    await renderAsAdmin()

    expect(screen.getAllByText('root').length).toBeGreaterThan(0)
    expect(within(rowOf('alice')).getByText('tenant')).toBeInTheDocument()
    expect(within(rowOf('root')).getByText('admin')).toBeInTheDocument()

    // alice（tenant）：禁用 + 提升为 admin；root（admin，未禁用，自己）：禁用 + 降级为 tenant
    expect(within(rowOf('alice')).getByRole('button', { name: /^禁\s*用$/ })).toBeInTheDocument()
    expect(within(rowOf('alice')).getByRole('button', { name: '提升为 admin' })).toBeInTheDocument()
    expect(within(rowOf('root')).getByRole('button', { name: /^禁\s*用$/ })).toBeInTheDocument()
    expect(within(rowOf('root')).getByRole('button', { name: '降级为 tenant' })).toBeInTheDocument()
  })

  it('禁用用户：二次确认后 PATCH 调用并刷新列表', async () => {
    const user = userEvent.setup()
    mockApi.adminUpdateUser.mockResolvedValue(undefined)
    mockApi.adminListUsers
      .mockResolvedValueOnce(users)
      .mockResolvedValueOnce([{ ...users[0], disabled: true }, users[1]])

    await renderAsAdmin()

    await user.click(within(rowOf('alice')).getByRole('button', { name: /^禁\s*用$/ }))
    expect(screen.getByText('确认禁用该用户？')).toBeInTheDocument()
    expect(mockApi.adminUpdateUser).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: '确认禁用' }))

    await waitFor(() => {
      expect(mockApi.adminUpdateUser).toHaveBeenCalledWith('alice', { disabled: true })
    })
    await screen.findByText('已禁用')
    expect(within(rowOf('alice')).getByRole('button', { name: /^启\s*用$/ })).toBeInTheDocument()
  })

  it('self_lockout：禁用自己被后端 409 拒绝时给出专门提示', async () => {
    const user = userEvent.setup()
    mockApi.adminUpdateUser.mockRejectedValue(
      new ConsoleApiError('资源冲突: cannot disable self', 409, 'self_lockout')
    )

    await renderAsAdmin()

    await user.click(within(rowOf('root')).getByRole('button', { name: /^禁\s*用$/ }))
    await user.click(screen.getByRole('button', { name: '确认禁用' }))

    expect(await screen.findByText(/不能禁用或降级当前登录的账号/)).toBeInTheDocument()
    // 列表保持原状（未误刷新出错误状态）
    expect(within(rowOf('root')).getByText('正常')).toBeInTheDocument()
  })

  it('角色调整：提升 tenant 为 admin 需二次确认后 PATCH {role:"admin"}', async () => {
    const user = userEvent.setup()
    mockApi.adminUpdateUser.mockResolvedValue(undefined)

    await renderAsAdmin()

    await user.click(within(rowOf('alice')).getByRole('button', { name: '提升为 admin' }))
    expect(screen.getByText('确认将 alice 提升为 admin？')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '确认提升' }))

    await waitFor(() => {
      expect(mockApi.adminUpdateUser).toHaveBeenCalledWith('alice', { role: 'admin' })
    })
  })

  it('角色调整：降级其他 admin 为 tenant 走 PATCH {role:"tenant"}', async () => {
    const user = userEvent.setup()
    const twoAdmins: AdminUser[] = [
      users[0],
      users[1],
      { id: 3, username: 'alice2', role: 'admin', disabled: false, created_at: null },
    ]
    mockApi.adminUpdateUser.mockResolvedValue(undefined)

    await renderAsAdmin(twoAdmins)

    await user.click(within(rowOf('alice2')).getByRole('button', { name: '降级为 tenant' }))
    expect(screen.getByText('确认将 alice2 降级为 tenant？')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '确认降级' }))

    await waitFor(() => {
      expect(mockApi.adminUpdateUser).toHaveBeenCalledWith('alice2', { role: 'tenant' })
    })
  })

  it('签发 tenant 邀请码：默认角色直接签发，payload 带 role:"tenant"', async () => {
    const user = userEvent.setup()
    mockApi.adminCreateInvite.mockResolvedValue({ code: 'dsh-abc123', max_uses: 1 })

    await renderAsAdmin()

    await user.click(screen.getByRole('button', { name: /^签\s*发$/ }))

    expect(await screen.findByText('邀请码签发成功')).toBeInTheDocument()
    expect(screen.getByText('dsh-abc123')).toBeInTheDocument()
    expect(mockApi.adminCreateInvite).toHaveBeenCalledWith({
      role: 'tenant',
      max_uses: undefined,
      expires_days: undefined,
    })
  })

  it('签发 admin 邀请码：选择 admin 角色后需二次确认，payload 带 role:"admin"', async () => {
    const user = userEvent.setup()
    mockApi.adminCreateInvite.mockResolvedValue({ code: 'dsh-admin9' })

    await renderAsAdmin()

    // 选择 admin 角色
    await clickRadio(user, 'admin')

    await user.click(screen.getByRole('button', { name: /^签\s*发$/ }))

    // 二次确认弹层出现，此时还未调用接口
    expect(screen.getByText('确认签发 admin 邀请码？')).toBeInTheDocument()
    expect(mockApi.adminCreateInvite).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: '确认签发' }))

    expect(await screen.findByText('邀请码签发成功')).toBeInTheDocument()
    expect(mockApi.adminCreateInvite).toHaveBeenCalledWith({
      role: 'admin',
      max_uses: undefined,
      expires_days: undefined,
    })
    expect(screen.getByText('dsh-admin9')).toBeInTheDocument()
  })

  it('签发 admin 邀请码：二次确认可取消，不调用接口', async () => {
    const user = userEvent.setup()

    await renderAsAdmin()

    await clickRadio(user, 'admin')
    await user.click(screen.getByRole('button', { name: /^签\s*发$/ }))

    // 二次确认弹层出现后取消
    expect(await screen.findByText('确认签发 admin 邀请码？')).toBeInTheDocument()
    await user.click(await screen.findByRole('button', { name: /^取\s*消$/ }))

    await waitFor(() => {
      expect(screen.queryByText('确认签发 admin 邀请码？')).not.toBeInTheDocument()
    })
    expect(mockApi.adminCreateInvite).not.toHaveBeenCalled()
  })

  it('非 admin 角色：显示 403 拒绝页，不调用管理端接口', async () => {
    mockApi.me.mockResolvedValue({ username: 'alice', role: 'tenant' })

    render(<Admin />)

    expect(await screen.findByText('仅管理员可访问')).toBeInTheDocument()
    expect(mockApi.adminListUsers).not.toHaveBeenCalled()
  })

  it('会话 401：跳转登录页（不再依赖 admin key）', async () => {
    mockApi.me.mockResolvedValue({ username: 'root', role: 'admin' })
    mockApi.adminListUsers.mockRejectedValue(new ConsoleApiError('未登录或会话已过期', 401))

    render(<Admin />)

    await waitFor(() => {
      expect(window.location.hash).toBe('#/login')
    })
  })
})
