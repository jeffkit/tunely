/**
 * 管理页（/admin，仅 role=admin 可见入口）
 * 契约 v1.1 §5/§6：管理端点鉴权为「admin key 或 admin 角色会话」，本页**只依赖会话 cookie**，
 * 不向前端下发或粘贴 admin key（admin key 仅供服务器脚本；既有默认路径的 key 管理台保持原样）。
 *
 * - GET  /api/console/admin/users：用户列表
 * - PATCH /api/console/admin/users/{username}：{disabled?, role?}；禁用/降级自己 → 409 self_lockout
 * - POST /api/console/admin/invites：{max_uses?, expires_days?, role?}；签发 admin 邀请需二次确认
 */
import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Form,
  InputNumber,
  Layout,
  Modal,
  Popconfirm,
  Radio,
  Result,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { CopyOutlined, ReloadOutlined } from '@ant-design/icons'
import { consoleApi } from '../api/console'
import { ConsoleApiError } from '../types/errors'
import { useSessionStore } from '../store/sessionStore'
import { navigate } from '../router'
import { copyTextToClipboard } from '../utils/clipboard'
import { formatDate } from '../utils/format'
import type { AdminInviteCreated, AdminUser, ConsoleRole } from '../types/console'

const { Header, Content } = Layout

interface InviteFormValues {
  role: ConsoleRole
  max_uses?: number
  expires_days?: number
}

export function Admin() {
  const { me, status, fetchMe, clear } = useSessionStore()

  const [users, setUsers] = useState<AdminUser[]>([])
  const [usersLoading, setUsersLoading] = useState(false)
  const [usersError, setUsersError] = useState<string | null>(null)
  const [togglingUsername, setTogglingUsername] = useState<string | null>(null)
  const [roleChangingUsername, setRoleChangingUsername] = useState<string | null>(null)

  const [inviting, setInviting] = useState(false)
  const [inviteResult, setInviteResult] = useState<AdminInviteCreated | null>(null)
  /** 选择 admin 角色后待二次确认的表单值 */
  const [pendingInvite, setPendingInvite] = useState<InviteFormValues | null>(null)

  const [form] = Form.useForm<InviteFormValues>()

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

  const isAdmin = status === 'authed' && me?.role === 'admin'

  /** 统一错误处理：401 会话失效回登录页；409 self_lockout 专门提示 */
  const handleApiError = useCallback(
    (err: unknown, fallback: string) => {
      if (err instanceof ConsoleApiError && err.statusCode === 401) {
        clear()
        navigate('/login', { replace: true })
        return
      }
      if (err instanceof ConsoleApiError && err.code === 'self_lockout') {
        message.error('操作被拒绝：不能禁用或降级当前登录的账号（self_lockout）')
        return
      }
      message.error(err instanceof Error ? err.message : fallback)
    },
    [clear]
  )

  const loadUsers = useCallback(async () => {
    setUsersLoading(true)
    setUsersError(null)
    try {
      const list = await consoleApi.adminListUsers()
      setUsers(list)
    } catch (err) {
      if (err instanceof ConsoleApiError && err.statusCode === 401) {
        // 会话失效/过期：回登录页（不再依赖 admin key）
        clear()
        navigate('/login', { replace: true })
        return
      }
      setUsersError(err instanceof Error ? err.message : '加载用户列表失败')
    } finally {
      setUsersLoading(false)
    }
  }, [clear])

  useEffect(() => {
    if (isAdmin) {
      void loadUsers()
    }
  }, [isAdmin, loadUsers])

  const handleToggleDisabled = async (user: AdminUser) => {
    setTogglingUsername(user.username)
    try {
      await consoleApi.adminUpdateUser(user.username, { disabled: !user.disabled })
      message.success(user.disabled ? `已启用 ${user.username}` : `已禁用 ${user.username}`)
      await loadUsers()
    } catch (err) {
      handleApiError(err, '操作失败，请稍后重试')
    } finally {
      setTogglingUsername(null)
    }
  }

  const handleRoleChange = async (user: AdminUser, role: ConsoleRole) => {
    setRoleChangingUsername(user.username)
    try {
      await consoleApi.adminUpdateUser(user.username, { role })
      message.success(`已将 ${user.username} 的角色调整为 ${role}`)
      await loadUsers()
    } catch (err) {
      handleApiError(err, '角色调整失败，请稍后重试')
    } finally {
      setRoleChangingUsername(null)
    }
  }

  const doCreateInvite = async (values: InviteFormValues) => {
    setInviting(true)
    try {
      const result = await consoleApi.adminCreateInvite({
        role: values.role,
        max_uses: values.max_uses,
        expires_days: values.expires_days,
      })
      form.resetFields()
      setInviteResult(result)
    } catch (err) {
      handleApiError(err, '签发失败，请稍后重试')
    } finally {
      setInviting(false)
    }
  }

  /** 表单提交：签发 admin 邀请需二次确认，tenant 直接签发 */
  const handleInviteFinish = (values: InviteFormValues) => {
    if (values.role === 'admin') {
      setPendingInvite(values)
      return
    }
    void doCreateInvite(values)
  }

  const handleCopyInviteCode = async () => {
    if (!inviteResult) return
    const ok = await copyTextToClipboard(inviteResult.code)
    if (ok) {
      message.success('邀请码已复制')
    } else {
      message.error('复制失败，请手动选择复制')
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

  if (me.role !== 'admin') {
    return (
      <Result
        status="403"
        title="仅管理员可访问"
        subTitle="该页面用于用户与邀请码管理，需要 admin 角色。"
        extra={<Button type="primary" onClick={() => navigate('/tunnels', { replace: true })}>返回我的隧道</Button>}
      />
    )
  }

  const columns: ColumnsType<AdminUser> = [
    { title: '用户名', dataIndex: 'username' },
    {
      title: '角色',
      dataIndex: 'role',
      width: 100,
      render: (role: string) =>
        role === 'admin' ? <Tag color="gold">admin</Tag> : <Tag>tenant</Tag>,
    },
    {
      title: '状态',
      dataIndex: 'disabled',
      width: 100,
      render: (disabled: boolean) =>
        disabled ? <Tag color="red">已禁用</Tag> : <Tag color="green">正常</Tag>,
    },
    {
      title: '隧道数',
      dataIndex: 'tunnel_count',
      width: 90,
      render: (count?: number) => (typeof count === 'number' ? count : '-'),
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
      width: 240,
      render: (_, record) => (
        <Space size="small">
          <Popconfirm
            title={record.disabled ? '确认启用该用户？' : '确认禁用该用户？'}
            description={
              record.disabled
                ? '启用后可重新登录并使用隧道。'
                : '禁用后会话立即失效，无法再登录。'
            }
            okText={record.disabled ? '确认启用' : '确认禁用'}
            okButtonProps={record.disabled ? undefined : { danger: true }}
            cancelText="取消"
            onConfirm={() => handleToggleDisabled(record)}
          >
            <Button type="link" size="small" loading={togglingUsername === record.username}>
              {record.disabled ? '启用' : '禁用'}
            </Button>
          </Popconfirm>
          {record.role === 'admin' ? (
            <Popconfirm
              title={`确认将 ${record.username} 降级为 tenant？`}
              description="降级后该账号将失去用户管理权限。"
              okText="确认降级"
              okButtonProps={{ danger: true }}
              cancelText="取消"
              onConfirm={() => handleRoleChange(record, 'tenant')}
            >
              <Button type="link" size="small" loading={roleChangingUsername === record.username}>
                降级为 tenant
              </Button>
            </Popconfirm>
          ) : (
            <Popconfirm
              title={`确认将 ${record.username} 提升为 admin？`}
              description="提升后该账号可管理用户与邀请码。"
              okText="确认提升"
              cancelText="取消"
              onConfirm={() => handleRoleChange(record, 'admin')}
            >
              <Button type="link" size="small" loading={roleChangingUsername === record.username}>
                提升为 admin
              </Button>
            </Popconfirm>
          )}
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
        <div style={{ color: '#fff', fontSize: 18, fontWeight: 'bold' }}>Tunely 控制台 - 用户管理</div>
        <Space size="middle">
          <span style={{ color: 'rgba(255, 255, 255, 0.85)' }}>{me.username}</span>
          <a href="#/tunnels" style={{ color: 'rgba(255, 255, 255, 0.85)' }}>
            我的隧道
          </a>
          <Button size="small" onClick={() => { clear(); navigate('/login', { replace: true }) }}>
            退出登录
          </Button>
        </Space>
      </Header>
      <Content style={{ padding: 24 }}>
        <div style={{ maxWidth: 1100, margin: '0 auto' }}>
          <Card title="签发邀请码" style={{ marginBottom: 24 }}>
            <Form
              form={form}
              layout="inline"
              onFinish={handleInviteFinish}
              initialValues={{ role: 'tenant' }}
            >
              <Form.Item name="role" label="角色">
                <Radio.Group
                  optionType="button"
                  options={[
                    { value: 'tenant', label: 'tenant' },
                    { value: 'admin', label: 'admin' },
                  ]}
                />
              </Form.Item>
              <Form.Item name="max_uses" label="最大使用次数">
                <InputNumber min={1} placeholder="默认 1" style={{ width: 120 }} />
              </Form.Item>
              <Form.Item name="expires_days" label="有效天数">
                <InputNumber min={1} placeholder="不限" style={{ width: 120 }} />
              </Form.Item>
              <Form.Item>
                <Button type="primary" htmlType="submit" loading={inviting}>
                  签发
                </Button>
              </Form.Item>
            </Form>
            <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
              邀请码签发后请立即复制保存，关闭弹层后不再完整展示；签发 admin 角色邀请需二次确认。
            </Typography.Paragraph>
          </Card>

          <Card
            title="用户列表"
            extra={
              <Button size="small" icon={<ReloadOutlined />} onClick={loadUsers}>
                刷新
              </Button>
            }
          >
            {usersError ? (
              <Alert type="warning" showIcon message="无法加载用户列表" description={usersError} />
            ) : (
              <Table
                rowKey="id"
                columns={columns}
                dataSource={users}
                loading={usersLoading}
                pagination={false}
                locale={{ emptyText: '暂无用户' }}
              />
            )}
          </Card>
        </div>
      </Content>

      {/* 签发 admin 邀请的二次确认 */}
      {pendingInvite && (
        <Modal
          open
          title="确认签发 admin 邀请码？"
          okText="确认签发"
          cancelText="取消"
          confirmLoading={inviting}
          onOk={() => {
            const values = pendingInvite
            setPendingInvite(null)
            void doCreateInvite(values)
          }}
          onCancel={() => setPendingInvite(null)}
        >
          <Alert
            type="warning"
            showIcon
            message="该邀请码注册的账号将拥有管理员权限（可管理用户与邀请码），请确认。"
          />
        </Modal>
      )}

      {inviteResult && (
        <Modal
          open
          title="邀请码签发成功"
          closable={false}
          keyboard={false}
          maskClosable={false}
          okText="我已保存"
          onOk={() => setInviteResult(null)}
        >
          <Space direction="vertical" style={{ width: '100%' }} size="middle">
            <Alert type="warning" showIcon message="请立即复制保存邀请码，关闭后不再完整展示。" />
            <Space>
              <code style={{ fontSize: 16, wordBreak: 'break-all' }}>{inviteResult.code}</code>
              <Button size="small" icon={<CopyOutlined />} onClick={handleCopyInviteCode}>
                复制
              </Button>
            </Space>
            {typeof inviteResult.max_uses === 'number' ? (
              <Typography.Text type="secondary">可使用 {inviteResult.max_uses} 次</Typography.Text>
            ) : null}
          </Space>
        </Modal>
      )}
    </Layout>
  )
}
