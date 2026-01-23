import { Card, Row, Col, Statistic } from 'antd'
import { CloudServerOutlined, CheckCircleOutlined, ApiOutlined } from '@ant-design/icons'
import type { Tunnel } from '../types'
import { formatNumber } from '../utils/format'

interface TunnelStatsProps {
  tunnels: Tunnel[]
}

export function TunnelStats({ tunnels }: TunnelStatsProps) {
  const total = tunnels.length
  const online = tunnels.filter((t) => t.connected && t.enabled).length
  const totalRequests = tunnels.reduce((sum, t) => sum + t.total_requests, 0)

  return (
    <Row gutter={16} style={{ marginBottom: 24 }}>
      <Col span={8}>
        <Card>
          <Statistic
            title="总隧道数"
            value={total}
            prefix={<CloudServerOutlined />}
            valueStyle={{ color: '#1890ff' }}
          />
        </Card>
      </Col>
      <Col span={8}>
        <Card>
          <Statistic
            title="在线隧道"
            value={online}
            prefix={<CheckCircleOutlined />}
            valueStyle={{ color: '#52c41a' }}
          />
        </Card>
      </Col>
      <Col span={8}>
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
    </Row>
  )
}
