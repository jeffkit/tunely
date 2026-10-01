/**
 * 接入二维码弹层测试
 * 契约 §6：二维码内容 = GET /api/console/entry 的 qr JSON 原样编码
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QrCodeModal } from './QrCodeModal'
import { consoleApi } from '../api/console'
import { renderQrSvg } from '../utils/qrcode'
import { copyTextToClipboard } from '../utils/clipboard'
import type { EntryInfo } from '../types/console'

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

vi.mock('../utils/qrcode', () => ({
  renderQrSvg: vi.fn(),
}))

vi.mock('../utils/clipboard', () => ({
  copyTextToClipboard: vi.fn(),
}))

const mockApi = vi.mocked(consoleApi)
const mockRenderQrSvg = vi.mocked(renderQrSvg)
const mockCopy = vi.mocked(copyTextToClipboard)

const entryInfo: EntryInfo = {
  entry_base: 'https://tun.example.com',
  qr: {
    v: 1,
    kind: 'dsh-tunnel',
    url: 'https://demo.example.com',
    desktop: 'tunely connect --token tun_x --target http://127.0.0.1:3080',
    note: '扫码接入',
  },
  desktop_connect: 'tunely connect --token tun_x --target http://127.0.0.1:3080',
}

describe('QrCodeModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockApi.getEntry.mockResolvedValue(entryInfo)
    mockRenderQrSvg.mockImplementation(
      async (content: string) => `<svg data-content="${content}"></svg>`
    )
  })

  it('打开时拉取 entry 并把 qr JSON 原样编码为二维码', async () => {
    render(<QrCodeModal open onClose={() => {}} />)

    expect(await screen.findByTestId('qr-svg')).toBeInTheDocument()

    expect(mockApi.getEntry).toHaveBeenCalledTimes(1)
    // 内容必须是 qr 对象原样 JSON，不做改写
    expect(mockRenderQrSvg).toHaveBeenCalledWith(JSON.stringify(entryInfo.qr))
  })

  it('展示接入地址与桌面端连接命令，并支持复制命令', async () => {
    const user = userEvent.setup()
    mockCopy.mockResolvedValue(true)

    render(<QrCodeModal open onClose={() => {}} />)

    expect(await screen.findByText('https://demo.example.com')).toBeInTheDocument()
    expect(screen.getByText(entryInfo.desktop_connect)).toBeInTheDocument()
    expect(screen.getByText('扫码接入')).toBeInTheDocument()

    await user.click(screen.getByText('复制连接命令'))
    expect(mockCopy).toHaveBeenCalledWith(entryInfo.desktop_connect)
  })

  it('entry 加载失败时给出提示且不渲染二维码', async () => {
    mockApi.getEntry.mockRejectedValue(new Error('服务暂时不可用: backend down'))

    render(<QrCodeModal open onClose={() => {}} />)

    expect(await screen.findByText(/backend down/)).toBeInTheDocument()
    expect(screen.queryByTestId('qr-svg')).not.toBeInTheDocument()
  })
})
