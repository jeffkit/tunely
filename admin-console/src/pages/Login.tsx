/**
 * 控制台登录页（/login）
 * 契约：POST /api/console/login → {username, role} + Set-Cookie；失败 401（服务端固定延迟 1s）
 */
import { useState } from 'react'
import { Button, Card, Form, Input, message, Typography } from 'antd'
import { consoleApi } from '../api/console'
import { useSessionStore } from '../store/sessionStore'
import { navigate } from '../router'

interface LoginFormValues {
  username: string
  password: string
}

export function Login() {
  const [form] = Form.useForm<LoginFormValues>()
  const [submitting, setSubmitting] = useState(false)
  const fetchMe = useSessionStore((s) => s.fetchMe)

  const handleSubmit = async (values: LoginFormValues) => {
    setSubmitting(true)
    try {
      await consoleApi.login(values)
      const me = await fetchMe()
      if (!me) {
        message.error('会话建立失败：账号可能已被禁用，请联系管理员')
        return
      }
      message.success(`欢迎回来，${me.username}`)
      navigate('/tunnels', { replace: true })
    } catch (err) {
      message.error(err instanceof Error ? err.message : '登录失败，请稍后重试')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: '#f0f2f5',
      }}
    >
      <Card style={{ width: 380 }}>
        <Typography.Title level={3} style={{ textAlign: 'center', marginBottom: 24 }}>
          Tunely 控制台
        </Typography.Title>
        <Form form={form} layout="vertical" onFinish={handleSubmit} requiredMark={false}>
          <Form.Item name="username" label="用户名" rules={[{ required: true, message: '请输入用户名' }]}>
            <Input placeholder="用户名" autoComplete="username" autoFocus />
          </Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: true, message: '请输入密码' }]}>
            <Input.Password placeholder="密码" autoComplete="current-password" />
          </Form.Item>
          <Form.Item style={{ marginBottom: 8 }}>
            <Button type="primary" htmlType="submit" block loading={submitting}>
              登录
            </Button>
          </Form.Item>
        </Form>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <Typography.Link href="#/register">没有账号？使用邀请码注册</Typography.Link>
          <Typography.Link href="#/legacy">admin key 管理台（旧版）</Typography.Link>
        </div>
      </Card>
    </div>
  )
}
