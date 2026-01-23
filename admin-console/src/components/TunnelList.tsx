import { useState } from 'react'
import {
  Table,
  Button,
  Space,
  Popconfirm,
  Tooltip,
  Modal,
} from 'antd'
import {
  EditOutlined,
  DeleteOutlined,
  ReloadOutlined,
  PlusOutlined,
  HistoryOutlined,
} from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import type { Tunnel } from '../types'
import { StatusBadge } from './StatusBadge'
import { formatDate, formatRelativeTime, formatNumber } from '../utils/format'
import { RequestLogs } from '../pages/RequestLogs'

interface TunnelListProps {
  tunnels: Tunnel[]
  loading: boolean
  onCreate: () => void
  onEdit: (tunnel: Tunnel) => void
  onDelete: (domain: string) => void
  onRegenerateToken: (domain: string) => Promise<{ domain: string; token: string } | void>
  onViewLogs?: (domain: string) => void
}

export function TunnelList({
  tunnels,
  loading,
  onCreate,
  onEdit,
  onDelete,
  onRegenerateToken,
  onViewLogs,
}: TunnelListProps) {
  const [logsModalVisible, setLogsModalVisible] = useState(false)
  const [selectedTunnelDomain, setSelectedTunnelDomain] = useState<string | null>(null)

  const handleRegenerateToken = async (domain: string) => {
    try {
      const result = await onRegenerateToken(domain)
      if (result?.token) {
        Modal.info({
          title: 'Token 重新生成成功',
          width: 600,
          content: (
            <div>
              <p>域名: <code>{result.domain}</code></p>
              <p>新 Token: <code>{result.token}</code></p>
              <p style={{ color: '#ff4d4f', marginTop: 16 }}>
                ⚠️ 请妥善保管新 Token，旧 Token 已失效！
              </p>
            </div>
          ),
          okText: '已保存',
        })
      }
    } catch (err) {
      // 错误已在 onRegenerateToken 中处理
    }
  }

  const columns: ColumnsType<Tunnel> = [
    {
      title: '域名',
      dataIndex: 'domain',
      key: 'domain',
      width: 200,
      render: (domain: string) => <code>{domain}</code>,
    },
    {
      title: '名称',
      dataIndex: 'name',
      key: 'name',
      width: 150,
      render: (name: string | null) => name || '-',
    },
    {
      title: '状态',
      key: 'status',
      width: 120,
      render: (_: any, record: Tunnel) => (
        <StatusBadge connected={record.connected} enabled={record.enabled} />
      ),
    },
    {
      title: '请求数',
      dataIndex: 'total_requests',
      key: 'total_requests',
      width: 100,
      align: 'right',
      render: (count: number) => formatNumber(count),
    },
    {
      title: '最后连接',
      dataIndex: 'last_connected_at',
      key: 'last_connected_at',
      width: 180,
      render: (date: string | null) => (
        <Tooltip title={formatDate(date)}>
          {formatRelativeTime(date)}
        </Tooltip>
      ),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 180,
      render: (date: string | null) => formatDate(date),
    },
    {
      title: '操作',
      key: 'actions',
      width: 200,
      fixed: 'right',
      render: (_: any, record: Tunnel) => (
        <Space size="small">
          <Button
            type="link"
            size="small"
            icon={<HistoryOutlined />}
            onClick={() => {
              if (onViewLogs) {
                onViewLogs(record.domain)
              } else {
                setSelectedTunnelDomain(record.domain)
                setLogsModalVisible(true)
              }
            }}
          >
            日志
          </Button>
          <Button
            type="link"
            size="small"
            icon={<EditOutlined />}
            onClick={() => onEdit(record)}
          >
            编辑
          </Button>
          <Button
            type="link"
            size="small"
            icon={<ReloadOutlined />}
            onClick={() => handleRegenerateToken(record.domain)}
          >
            重新生成 Token
          </Button>
          <Popconfirm
            title="确定要删除这个隧道吗？"
            description="删除后无法恢复，请谨慎操作。"
            onConfirm={() => onDelete(record.domain)}
            okText="确定"
            cancelText="取消"
          >
            <Button type="link" size="small" danger icon={<DeleteOutlined />}>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <div>
      <div style={{ marginBottom: 16, display: 'flex', justifyContent: 'space-between' }}>
        <h2 style={{ margin: 0 }}>隧道列表</h2>
        <Button type="primary" icon={<PlusOutlined />} onClick={onCreate}>
          创建隧道
        </Button>
      </div>
      <Table
        columns={columns}
        dataSource={tunnels}
        loading={loading}
        rowKey="domain"
        scroll={{ x: 1200 }}
        pagination={{
          pageSize: 20,
          showSizeChanger: true,
          showTotal: (total) => `共 ${total} 条`,
        }}
      />
      
      <Modal
        title={`请求历史 - ${selectedTunnelDomain}`}
        open={logsModalVisible}
        onCancel={() => {
          setLogsModalVisible(false)
          setSelectedTunnelDomain(null)
        }}
        footer={null}
        width="90%"
        style={{ top: 20 }}
      >
        <RequestLogs tunnelDomain={selectedTunnelDomain} />
      </Modal>
    </div>
  )
}
