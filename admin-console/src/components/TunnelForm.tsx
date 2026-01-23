import { Modal, Form, Input, Switch } from 'antd'
import { useEffect } from 'react'
import type { Tunnel, CreateTunnelRequest, UpdateTunnelRequest } from '../types'

interface TunnelFormProps {
  open: boolean
  tunnel?: Tunnel | null
  onCancel: () => void
  onSuccess: () => void
  onSubmit: (data: CreateTunnelRequest | UpdateTunnelRequest) => Promise<void>
}

export function TunnelForm({ open, tunnel, onCancel, onSuccess, onSubmit }: TunnelFormProps) {
  const [form] = Form.useForm()

  useEffect(() => {
    if (open) {
      if (tunnel) {
        form.setFieldsValue({
          domain: tunnel.domain,
          name: tunnel.name || '',
          description: tunnel.description || '',
          enabled: tunnel.enabled,
        })
      } else {
        form.resetFields()
      }
    }
  }, [open, tunnel, form])

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields()
      await onSubmit(values)
      form.resetFields()
      onSuccess()
    } catch (err: any) {
      // 表单验证错误或提交错误
      if (err?.errorFields) {
        // 表单验证错误，不需要处理
        return
      }
      // 提交错误已在 onSubmit 中处理
    }
  }

  const isEdit = !!tunnel

  return (
    <Modal
      title={isEdit ? '编辑隧道' : '创建隧道'}
      open={open}
      onOk={handleSubmit}
      onCancel={onCancel}
      okText="确定"
      cancelText="取消"
      destroyOnClose
    >
      <Form form={form} layout="vertical">
        {!isEdit && (
          <Form.Item
            name="domain"
            label="域名"
            rules={[
              { required: true, message: '请输入域名' },
              {
                pattern: /^[a-zA-Z0-9][-a-zA-Z0-9]{0,62}$/,
                message: '域名格式不正确（字母数字开头，可包含中划线，1-63字符）',
              },
            ]}
          >
            <Input placeholder="例如: my-agent" />
          </Form.Item>
        )}
        <Form.Item name="name" label="名称">
          <Input placeholder="隧道名称（可选）" />
        </Form.Item>
        <Form.Item name="description" label="描述">
          <Input.TextArea rows={3} placeholder="隧道描述（可选）" />
        </Form.Item>
        {isEdit && (
          <Form.Item name="enabled" label="状态" valuePropName="checked">
            <Switch checkedChildren="启用" unCheckedChildren="禁用" />
          </Form.Item>
        )}
      </Form>
    </Modal>
  )
}
