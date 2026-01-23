import { Card, Descriptions, Tag } from 'antd'
import type { ServerInfo } from '../types'

interface SystemInfoProps {
  info: ServerInfo | null
  loading: boolean
}

export function SystemInfo({ info, loading }: SystemInfoProps) {
  if (loading || !info) {
    return <Card title="系统信息" loading={loading} />
  }

  return (
    <Card title="系统信息">
      <Descriptions column={1} bordered>
        <Descriptions.Item label="服务名称">{info.name}</Descriptions.Item>
        <Descriptions.Item label="版本">
          <Tag color="blue">{info.version}</Tag>
        </Descriptions.Item>
        <Descriptions.Item label="域名模式">
          <code>{info.domain.pattern}</code>
        </Descriptions.Item>
        <Descriptions.Item label="WebSocket URL">
          <code>{info.websocket.url}</code>
        </Descriptions.Item>
        <Descriptions.Item label="支持的协议">
          {info.protocols.map((p) => (
            <Tag key={p} color="green">
              {p.toUpperCase()}
            </Tag>
          ))}
        </Descriptions.Item>
      </Descriptions>
    </Card>
  )
}
