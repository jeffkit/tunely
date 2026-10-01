/**
 * 租户主页（/tunnels）
 * 契约 §6：隧道列表（在线徽标、流量）、新建（前缀 → 域名 + token 一次性展示）、
 * rotate、删除（二次确认）、接入二维码弹层。
 */
import { useCallback, useEffect, useState } from 'react'
import { Badge, Button, Card, Input, Layout, Popconfirm, Space, Spin, Table, Typography, message } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ReloadOutlined } from '@ant-design/icons'
import { consoleApi } from '../api/console'
import { ConsoleApiError } from '../types/errors'
import { useSessionStore } from '../store/sessionStore'
import { navigate } from '../router'
import { formatBytes, formatDate, formatRelativeTime } from '../utils/format'
import { TokenRevealModal } from '../components/TokenRevealModal'
import { QrCodeModal } from '../components/QrCodeModal'
import type { TenantTunnel } from '../types/console'

const { Header, Content } = Layout

interface TokenReveal {
  title: string
  domain: string
  token: string
}

export function MyTunnels() {
  const { me, status, fetchMe, clear } = useSessionStore()

  const [tunnels, setTunnels] = useState<TenantTunnel[]>([])
  const [loading, setLoading] = useState(false)

  const [prefix, setPrefix] = useState('')
  const [creating, setCreating] = useState(false)

  const [rotatingDomain, setRotatingDomain] = useState<string | null>(null)
  const [deletingDomain, setDeletingDomain] = useState<string | null>(null)
  const [loggingOut, setLoggingOut] = useState(false)

  /** token 明文仅存在于该状态中，确认「我已保存」后立即清除 */
  const [reveal, setReveal] = useState<TokenReveal | null>(null)
  const [qrOpen, setQrOpen] = useState(false)

  // 会话守卫：未知 → 查询；未登录 → 回登录页
  useEffect(() => {
    if (status === 'unknown') {
      void fetchMe()
    }
  }, [status, fetchMe])

  useEffect(() => {
    if (status === 'anonymous') {
      navigate('/login', { replace: true })
    }
  }, [status])

  const handleApiError = useCallback(
    (err: unknown, fallback: string) => {
      if (err instanceof ConsoleApiError && err.statusCode === 401) {
        // 会话过期，回登录页
        clear()
        navigate('/login', { replace: true })
        return
      }
      if (err instanceof ConsoleApiError && err.code === 'quota_exceeded') {
        message.warning('已达配额上限：无法创建更多隧道，请先删除不用的隧道')
        return
      }
      message.error(err instanceof Error ? err.message : fallback)
    },
    [clear]
  )

  const loadTunnels = useCallback(async () => {
    setLoading(true)
    try {
      const list = await consoleApi.listMyTunnels()
      setTunnels(list)
    } catch (err) {
      handleApiError(err, '加载隧道列表失败')
    } finally {
      setLoading(false)
    }
  }, [handleApiError])

  useEffect(() => {
    if (status === 'authed') {
      void loadTunnels()
    }
  }, [status, loadTunnels])

  const handleCreate = async () => {
    const trimmed = prefix.trim()
    if (!trimmed) {
      message.warning('请输入隧道前缀')
      return
    }
    setCreating(true)
    try {
      // 契约：token 明文仅创建/rotate 响应返回，此后不可再获取
      const created = await consoleApi.createMyTunnel(trimmed)
      setPrefix('')
      setReveal({ title: '隧道创建成功', domain: created.domain, token: created.token })
      await loadTunnels()
    } catch (err) {
      handleApiError(err, '创建失败，请稍后重试')
    } finally {
      setCreating(false)
    }
  }

  const handleRotate = async (domain: string) => {
    setRotatingDomain(domain)
    try {
      const result = await consoleApi.rotateTunnelToken(domain)
      setReveal({ title: 'Token 已轮换', domain: result.domain, token: result.token })
      await loadTunnels()
    } catch (err) {
      handleApiError(err, '轮换失败，请稍后重试')
    } finally {
      setRotatingDomain(null)
    }
  }

  const handleDelete = async (domain: string) => {
    setDeletingDomain(domain)
    try {
      await consoleApi.deleteMyTunnel(domain)
      message.success(`隧道 ${domain} 已删除`)
      await loadTunnels()
    } catch (err) {
      handleApiError(err, '删除失败，请稍后重试')
    } finally {
      setDeletingDomain(null)
    }
  }

  const handleLogout = async () => {
    setLoggingOut(true)
    try {
      await consoleApi.logout()
    } catch {
      // 登出接口失败也照常清理本地会话状态
    } finally {
      clear()
      setLoggingOut(false)
      navigate('/login', { replace: true })
    }
  }

  if (status !== 'authed' || !me) {
    return (
      <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ textAlign: 'center' }}>
          <Spin />
          <Typography.Text type="secondary" style={{ display: 'block', marginTop: 12 }}>
            加载会话...
          </Typography.Text>
        </div>
      </div>
    )
  }

  const columns: ColumnsType<TenantTunnel> = [
    {
      title: '域名',
      dataIndex: 'domain',
      render: (domain: string) => <code>{domain}</code>,
    },
    {
      title: '状态',
      dataIndex: 'online',
      width: 100,
      render: (online: boolean) =>
        online ? <Badge status="success" text="在线" /> : <Badge status="error" text="离线" />,
    },
    {
      title: '流量（入 / 出）',
      dataIndex: 'bytes_in',
      width: 200,
      render: (_, record) => (
        <span>
          ↑ {formatBytes(record.bytes_in ?? null)} / ↓ {formatBytes(record.bytes_out ?? null)}
        </span>
      ),
    },
    {
      title: '最后活跃',
      dataIndex: 'last_seen_at',
      width: 130,
      render: (value: string | null) => (
        <Typography.Text type="secondary">{formatRelativeTime(value ?? null)}</Typography.Text>
      ),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 180,
      render: (value: string | null) => (
        <Typography.Text type="secondary">{formatDate(value ?? null)}</Typography.Text>
      ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 260,
      render: (_, record) => (
        <Space size="small">
          <Button type="link" size="small" onClick={() => setQrOpen(true)}>
            接入二维码
          </Button>
          <Popconfirm
            title="确认轮换 Token？"
            description="旧 Token 将立即失效，在线连接会被断开。"
            okText="确认轮换"
            cancelText="取消"
            onConfirm={() => handleRotate(record.domain)}
          >
            <Button type="link" size="small" loading={rotatingDomain === record.domain}>
              轮换 Token
            </Button>
          </Popconfirm>
          <Popconfirm
            title="确认删除该隧道？"
            description="删除后不可恢复，在线连接将立即断开。"
            okText="确认删除"
            okButtonProps={{ danger: true }}
            cancelText="取消"
            onConfirm={() => handleDelete(record.domain)}
          >
            <Button type="link" size="small" danger loading={deletingDomain === record.domain}>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header
        style={{
          background: '#001529',
          padding: '0 24px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}
      >
        <div style={{ color: '#fff', fontSize: 18, fontWeight: 'bold' }}>Tunely 控制台</div>
        <Space size="middle">
          <span style={{ color: 'rgba(255, 255, 255, 0.85)' }}>{me.username}</span>
          {me.role === 'admin' && (
            <a href="#/admin" style={{ color: 'rgba(255, 255, 255, 0.85)' }}>
              用户管理
            </a>
          )}
          <Button size="small" onClick={handleLogout} loading={loggingOut}>
            退出登录
          </Button>
        </Space>
      </Header>
      <Content style={{ padding: 24 }}>
        <div style={{ maxWidth: 1100, margin: '0 auto' }}>
          <Card title="新建隧道" style={{ marginBottom: 24 }}>
            <Space.Compact style={{ width: '100%', maxWidth: 480 }}>
              <Input
                placeholder="隧道前缀（如 my-app）"
                value={prefix}
                onChange={(e) => setPrefix(e.target.value)}
                onPressEnter={handleCreate}
                maxLength={63}
              />
              <Button type="primary" loading={creating} onClick={handleCreate}>
                创建隧道
              </Button>
            </Space.Compact>
            <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
              创建成功后 Token 仅展示一次，请立即保存；每个账号可创建的隧道数量受配额限制。
            </Typography.Paragraph>
          </Card>

          <Card
            title="我的隧道"
            extra={
              <Button size="small" icon={<ReloadOutlined />} onClick={loadTunnels}>
                刷新
              </Button>
            }
          >
            <Table
              rowKey="domain"
              columns={columns}
              dataSource={tunnels}
              loading={loading}
              pagination={false}
              locale={{ emptyText: '暂无隧道，先创建一个吧' }}
            />
          </Card>
        </div>
      </Content>

      {reveal && (
        <TokenRevealModal
          open
          title={reveal.title}
          domain={reveal.domain}
          token={reveal.token}
          onConfirmed={() => setReveal(null)}
        />
      )}
      <QrCodeModal open={qrOpen} onClose={() => setQrOpen(false)} />
    </Layout>
  )
}
