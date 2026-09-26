import { Card, Row, Col, Statistic } from 'antd'
import { CloudServerOutlined, CheckCircleOutlined, ApiOutlined, ThunderboltOutlined } from '@ant-design/icons'
import type { Tunnel } from '../types'
import { formatNumber, formatBytes } from '../utils/format'

interface TunnelStatsProps {
  tunnels: Tunnel[]
}

export function TunnelStats({ tunnels }: TunnelStatsProps) {
  const total = tunnels.length
  const online = tunnels.filter((t) => t.connected && t.enabled).length
  const totalRequests = tunnels.reduce((sum, t) => sum + (t.total_requests ?? 0), 0)
  const totalBytes = tunnels.reduce(
    (sum, t) => sum + (t.bytes_in ?? 0) + (t.bytes_out ?? 0),
    0
  )

  return (
    <Row gutter={16} style={{ marginBottom: 24 }}>
      <Col span={6}>
        <Card>
          <Statistic
            title="总隧道数"
            value={total}
            prefix={<CloudServerOutlined />}
            valueStyle={{ color: '#1890ff' }}
          />
        </Card>
      </Col>
      <Col span={6}>
        <Card>
          <Statistic
            title="在线隧道"
            value={online}
            prefix={<CheckCircleOutlined />}
            valueStyle={{ color: '#52c41a' }}
          />
        </Card>
      </Col>
      <Col span={6}>
        <Card>
          <Statistic
            title="总请求数"
            value={totalRequests}
            prefix={<ApiOutlined />}
            formatter={(value) => formatNumber(Number(value))}
            valueStyle={{ color: '#722ed1' }}
          />
        </Card>
      </Col>
      <Col span={6}>
        <Card>
          <Statistic
            title="总流量"
            value={totalBytes}
            prefix={<ThunderboltOutlined />}
            formatter={(value) => formatBytes(Number(value))}
            valueStyle={{ color: '#13c2c2' }}
          />
        </Card>
      </Col>
    </Row>
  )
}
