/**
 * 控制台注册页（/register）
 * 契约：POST /api/console/register {username,password,invite_code} → 201 {username, role}
 * 用户名规则（数据模型）：3-32 位，[a-z0-9_-]
 */
import { useState } from 'react'
import { Button, Card, Form, Input, message, Typography } from 'antd'
import { consoleApi } from '../api/console'
import { navigate } from '../router'

interface RegisterFormValues {
  username: string
  password: string
  confirm: string
  invite_code: string
}

const USERNAME_PATTERN = /^[a-z0-9_-]{3,32}$/

export function Register() {
  const [form] = Form.useForm<RegisterFormValues>()
  const [submitting, setSubmitting] = useState(false)

  const handleSubmit = async (values: RegisterFormValues) => {
    setSubmitting(true)
    try {
      const result = await consoleApi.register({
        username: values.username,
        password: values.password,
        invite_code: values.invite_code,
      })
      message.success(`注册成功：${result.username}，请登录`)
      navigate('/login', { replace: true })
    } catch (err) {
      message.error(err instanceof Error ? err.message : '注册失败，请稍后重试')
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
        <Typography.Title level={3} style={{ textAlign: 'center', marginBottom: 8 }}>
          注册租户账号
        </Typography.Title>
        <Typography.Paragraph type="secondary" style={{ textAlign: 'center' }}>
          注册需要有效的邀请码
        </Typography.Paragraph>
        <Form form={form} layout="vertical" onFinish={handleSubmit} requiredMark={false}>
          <Form.Item
            name="username"
            label="用户名"
            rules={[
              { required: true, message: '请输入用户名' },
              {
                pattern: USERNAME_PATTERN,
                message: '3-32 位，仅限小写字母、数字、下划线和中划线',
              },
            ]}
          >
            <Input placeholder="3-32 位小写字母/数字/_/-" autoComplete="username" autoFocus />
          </Form.Item>
          <Form.Item
            name="password"
            label="密码"
            rules={[
              { required: true, message: '请输入密码' },
              { min: 8, message: '密码至少 8 位' },
            ]}
          >
            <Input.Password placeholder="至少 8 位" autoComplete="new-password" />
          </Form.Item>
          <Form.Item
            name="confirm"
            label="确认密码"
            dependencies={['password']}
            rules={[
              { required: true, message: '请再次输入密码' },
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (!value || getFieldValue('password') === value) {
                    return Promise.resolve()
                  }
                  return Promise.reject(new Error('两次输入的密码不一致'))
                },
              }),
            ]}
          >
            <Input.Password placeholder="再次输入密码" autoComplete="new-password" />
          </Form.Item>
          <Form.Item
            name="invite_code"
            label="邀请码"
            rules={[{ required: true, message: '请输入邀请码' }]}
          >
            <Input placeholder="如 dsh-xxxxxx" autoComplete="off" />
          </Form.Item>
          <Form.Item style={{ marginBottom: 8 }}>
            <Button type="primary" htmlType="submit" block loading={submitting}>
              注册
            </Button>
          </Form.Item>
        </Form>
        <div style={{ textAlign: 'center' }}>
          <Typography.Link href="#/login">已有账号？直接登录</Typography.Link>
        </div>
      </Card>
    </div>
  )
}
