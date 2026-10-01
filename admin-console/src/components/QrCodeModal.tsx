/**
 * 接入二维码弹层
 * 契约 §6：内容 = GET /api/console/entry 返回的 qr JSON 原样（JSON.stringify(qr)）编码；
 * 同时展示接入 URL 与桌面端连接命令（可复制）。
 */
import { useEffect, useState } from 'react'
import { Alert, Descriptions, Modal, Space, Spin, Typography, message } from 'antd'
import { copyTextToClipboard } from '../utils/clipboard'
import { renderQrSvg } from '../utils/qrcode'
import { consoleApi } from '../api/console'
import type { EntryInfo } from '../types/console'

interface QrCodeModalProps {
  open: boolean
  onClose: () => void
}

export function QrCodeModal({ open, onClose }: QrCodeModalProps) {
  const [loading, setLoading] = useState(false)
  const [entry, setEntry] = useState<EntryInfo | null>(null)
  const [qrSvg, setQrSvg] = useState('')

  useEffect(() => {
    if (!open) return
    let cancelled = false
    setLoading(true)
    consoleApi
      .getEntry()
      .then(async (info) => {
        // qr 对象原样编码，不改写任何字段
        const svg = await renderQrSvg(JSON.stringify(info.qr))
        if (!cancelled) {
          setEntry(info)
          setQrSvg(svg)
        }
      })
      .catch((err) => {
        if (!cancelled) {
          message.error(err instanceof Error ? err.message : '加载接入信息失败')
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [open])

  const handleCopyDesktop = async () => {
    if (!entry) return
    const ok = await copyTextToClipboard(entry.desktop_connect)
    if (ok) {
      message.success('连接命令已复制')
    } else {
      message.error('复制失败，请手动选择复制')
    }
  }

  return (
    <Modal
      open={open}
      title="接入二维码"
      onCancel={onClose}
      footer={null}
      width={480}
      destroyOnHidden
    >
      {loading ? (
        <div style={{ textAlign: 'center', padding: 32 }}>
          <Spin />
          <Typography.Text type="secondary" style={{ display: 'block', marginTop: 12 }}>
            加载接入信息...
          </Typography.Text>
        </div>
      ) : entry ? (
        <Space direction="vertical" style={{ width: '100%' }} size="middle">
          {entry.qr?.note ? <Alert type="info" showIcon message={entry.qr.note} /> : null}
          <div style={{ textAlign: 'center' }}>
            {/* 二维码内容为 entry.qr JSON 原样（v/kind/url/desktop[/note]） */}
            <div
              data-testid="qr-svg"
              style={{ display: 'inline-block' }}
              dangerouslySetInnerHTML={{ __html: qrSvg }}
            />
            <Typography.Text type="secondary">
              使用 tunely 客户端扫码或复制下方命令接入
            </Typography.Text>
          </div>
          <Descriptions bordered column={1} size="small">
            <Descriptions.Item label="接入地址">
              <Typography.Link href={entry.qr?.url} target="_blank">
                {entry.qr?.url || entry.entry_base}
              </Typography.Link>
            </Descriptions.Item>
            <Descriptions.Item label="桌面端命令">
              <Space direction="vertical" size={4} style={{ width: '100%' }}>
                <code style={{ wordBreak: 'break-all' }}>{entry.desktop_connect}</code>
                <Typography.Link onClick={handleCopyDesktop}>复制连接命令</Typography.Link>
              </Space>
            </Descriptions.Item>
          </Descriptions>
        </Space>
      ) : (
        <Typography.Text type="secondary">暂无接入信息</Typography.Text>
      )}
    </Modal>
  )
}
