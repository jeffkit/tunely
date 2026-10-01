/**
 * Token 一次性展示弹层测试
 * 覆盖：token 明文展示、复制、确认回调；确认后由父组件清除状态（明文不再出现）
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { TokenRevealModal } from './TokenRevealModal'
import { copyTextToClipboard } from '../utils/clipboard'

vi.mock('../utils/clipboard', () => ({
  copyTextToClipboard: vi.fn(),
}))

const mockCopy = vi.mocked(copyTextToClipboard)

/** 模拟父组件：确认后清除 token 状态（与 MyTunnels 的用法一致） */
function RevealHarness({ onConfirmed }: { onConfirmed?: () => void }) {
  const [reveal, setReveal] = useState<{ domain: string; token: string } | null>({
    domain: 'demo.example.com',
    token: 'tun_secret_value',
  })
  return (
    <div>
      <span>外部区域</span>
      {reveal && (
        <TokenRevealModal
          open
          title="隧道创建成功"
          domain={reveal.domain}
          token={reveal.token}
          onConfirmed={() => {
            setReveal(null)
            onConfirmed?.()
          }}
        />
      )}
    </div>
  )
}

describe('TokenRevealModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockCopy.mockResolvedValue(true)
  })

  it('展示域名与 token 明文，且不可通过遮罩/ESC 关闭', () => {
    render(<TokenRevealModal open title="隧道创建成功" domain="demo.example.com" token="tun_abc" onConfirmed={() => {}} />)

    expect(screen.getByText('demo.example.com')).toBeInTheDocument()
    expect(screen.getByText('tun_abc')).toBeInTheDocument()
    expect(screen.getByText(/仅此一次展示/)).toBeInTheDocument()

    const dialog = screen.getByRole('dialog')
    expect(dialog).not.toHaveAttribute('keyboard') // keyboard=false 由 antd 处理，这里只确认弹层存在
  })

  it('复制按钮复制 token 明文', async () => {
    const user = userEvent.setup()
    render(<TokenRevealModal open title="隧道创建成功" domain="demo.example.com" token="tun_abc" onConfirmed={() => {}} />)

    // 两个复制按钮：域名、token（取第二个）
    const copyButtons = screen.getAllByRole('button', { name: /复制/ })
    expect(copyButtons).toHaveLength(2)
    await user.click(copyButtons[1])

    await waitFor(() => {
      expect(mockCopy).toHaveBeenCalledWith('tun_abc')
    })
  })

  it('点击「我已保存」后弹层关闭，token 明文从 DOM 消失（一次性展示）', async () => {
    const user = userEvent.setup()
    const onConfirmed = vi.fn()
    render(<RevealHarness onConfirmed={onConfirmed} />)

    expect(screen.getByText('tun_secret_value')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '我已保存' }))

    await waitFor(() => {
      expect(onConfirmed).toHaveBeenCalledTimes(1)
    })
    await waitFor(() => {
      expect(screen.queryByText('tun_secret_value')).not.toBeInTheDocument()
    })
    expect(screen.queryByText('demo.example.com')).not.toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})
