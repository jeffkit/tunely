/**
 * Token 一次性展示弹层
 * 契约 §6：token 明文展示一次 + 复制按钮 + 「我已保存」确认；
 * 关闭（确认）后父组件必须清除 token 状态，明文不再出现。
 * 弹层不可通过遮罩/ESC/关闭钮绕过，只能点「我已保存」离开。
 */
import { useState } from 'react'
import { Alert, Button, Descriptions, Modal, Space, message } from 'antd'
import { CopyOutlined } from '@ant-design/icons'
import { copyTextToClipboard } from '../utils/clipboard'

interface TokenRevealModalProps {
  open: boolean
  /** 弹层标题（区分「创建成功」/「Token 已轮换」） */
  title: string
  domain: string
  token: string
  /** 点击「我已保存」：父组件负责清除 token 并关闭弹层 */
  onConfirmed: () => void
}

export function TokenRevealModal({ open, title, domain, token, onConfirmed }: TokenRevealModalProps) {
  const [copiedToken, setCopiedToken] = useState(false)

  const handleCopy = async (text: string, isToken: boolean) => {
    const ok = await copyTextToClipboard(text)
    if (ok) {
      message.success('已复制到剪贴板')
      if (isToken) setCopiedToken(true)
    } else {
      message.error('复制失败，请手动选择复制')
    }
  }

  return (
    <Modal
      open={open}
      title={title}
      closable={false}
      keyboard={false}
      maskClosable={false}
      footer={[
        <Button key="confirm" type="primary" onClick={onConfirmed}>
          我已保存
        </Button>,
      ]}
    >
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        <Alert
          type="warning"
          showIcon
          message="Token 明文仅此一次展示，关闭后无法再次查看，请立即妥善保存。"
        />
        <Descriptions bordered column={1} size="small">
          <Descriptions.Item label="域名">
            <Space>
              <code>{domain}</code>
              <Button size="small" icon={<CopyOutlined />} onClick={() => handleCopy(domain, false)}>
                复制
              </Button>
            </Space>
          </Descriptions.Item>
          <Descriptions.Item label="Token">
            <Space>
              <code style={{ wordBreak: 'break-all' }}>{token}</code>
              <Button
                size="small"
                icon={<CopyOutlined />}
                onClick={() => handleCopy(token, true)}
              >
                {copiedToken ? '已复制' : '复制'}
              </Button>
            </Space>
          </Descriptions.Item>
        </Descriptions>
      </Space>
    </Modal>
  )
}
