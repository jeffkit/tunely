/**
 * 后端配置管理组件
 * 
 * 用于管理多个后端服务器配置，支持添加、编辑、删除、切换
 */
import { useState } from 'react'
import { Select, Button, Modal, Form, Input, Space, Popconfirm, message } from 'antd'
import { PlusOutlined, EditOutlined, DeleteOutlined, SettingOutlined } from '@ant-design/icons'
import {
  getAllBackendConfigs,
  getCurrentBackendConfig,
  getCurrentBackendId,
  setCurrentBackendId,
  saveBackendConfig,
  deleteBackendConfig,
  generateBackendId,
  type BackendConfig,
} from '../utils/backendConfig'
import { refreshClient } from '../api/client'

export function BackendConfigManager() {
  const [backendConfigs, setBackendConfigs] = useState<BackendConfig[]>(() => getAllBackendConfigs())
  const [currentBackend, setCurrentBackend] = useState<BackendConfig | null>(() => getCurrentBackendConfig())
  const [configModalVisible, setConfigModalVisible] = useState(false)
  const [editingConfig, setEditingConfig] = useState<BackendConfig | null>(null)
  const [form] = Form.useForm()

  // 加载后端配置
  const loadBackendConfigs = () => {
    const configs = getAllBackendConfigs()
    setBackendConfigs(configs)
    const current = getCurrentBackendConfig()
    setCurrentBackend(current)
  }

  // 切换后端
  const handleSwitchBackend = (backendId: string | null) => {
    if (!backendId) {
      setCurrentBackendId(null)
      setCurrentBackend(null)
      refreshClient()
      message.info('已切换到默认后端')
      return
    }
    setCurrentBackendId(backendId)
    loadBackendConfigs()
    refreshClient()
    message.success('后端切换成功')
  }

  // 打开添加模态框
  const handleOpenAddModal = () => {
    setEditingConfig(null)
    form.resetFields()
    form.setFieldsValue({
      name: '',
      baseUrl: '',
      apiKey: '',
    })
    setConfigModalVisible(true)
  }

  // 打开编辑模态框
  const handleOpenEditModal = (config: BackendConfig) => {
    setEditingConfig(config)
    form.setFieldsValue({
      name: config.name,
      baseUrl: config.baseUrl,
      apiKey: config.apiKey,
    })
    setConfigModalVisible(true)
  }

  // 保存配置
  const handleSaveConfig = async () => {
    try {
      const values = await form.validateFields()
      
      // 预先 trim，避免保存时丢失数据
      const baseUrl = values.baseUrl?.trim()
      const apiKey = values.apiKey?.trim()
      
      // 验证 trim 后不为空
      if (!baseUrl) {
        message.error('后端服务地址不能为空')
        return
      }
      if (!apiKey) {
        message.error('API Key 不能为空')
        return
      }
      
      const config: BackendConfig = {
        id: editingConfig?.id || generateBackendId(),
        name: values.name.trim(),
        baseUrl,
        apiKey,
      }
      saveBackendConfig(config)
      loadBackendConfigs()

      // 如果是新配置或当前配置，自动切换
      if (!editingConfig || editingConfig.id === getCurrentBackendId()) {
        handleSwitchBackend(config.id)
      }

      message.success(editingConfig ? '后端配置已更新' : '后端配置已添加')
      setConfigModalVisible(false)
      form.resetFields()
    } catch (error) {
      // 表单验证失败
    }
  }

  // 删除配置
  const handleDeleteConfig = (id: string) => {
    deleteBackendConfig(id)
    loadBackendConfigs()
    message.success('后端配置已删除')
  }

  return (
    <>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <Select
          value={currentBackend?.id || null}
          onChange={handleSwitchBackend}
          placeholder="选择后端"
          style={{ width: 200 }}
          size="small"
          dropdownRender={(menu) => (
            <>
              {menu}
              <div style={{ padding: '8px', borderTop: '1px solid #d9d9d9' }}>
                <Button
                  type="link"
                  icon={<PlusOutlined />}
                  onClick={handleOpenAddModal}
                  style={{ width: '100%', textAlign: 'left', padding: 0 }}
                >
                  添加后端配置
                </Button>
              </div>
            </>
          )}
        >
          {backendConfigs.map((config) => (
            <Select.Option key={config.id} value={config.id}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span>{config.name}</span>
                <Space size="small" onClick={(e) => e.stopPropagation()}>
                  <Button
                    type="text"
                    size="small"
                    icon={<EditOutlined />}
                    onClick={() => handleOpenEditModal(config)}
                    style={{ color: '#1890ff' }}
                  />
                  <Popconfirm
                    title="确定删除这个后端配置吗？"
                    onConfirm={() => handleDeleteConfig(config.id)}
                    okText="确定"
                    cancelText="取消"
                  >
                    <Button type="text" size="small" icon={<DeleteOutlined />} danger />
                  </Popconfirm>
                </Space>
              </div>
            </Select.Option>
          ))}
        </Select>
        {currentBackend && (
          <span style={{ color: '#fff', marginRight: 8, fontSize: 12 }}>
            <code style={{ background: 'rgba(255,255,255,0.1)', padding: '2px 6px', borderRadius: 2 }}>
              {currentBackend.baseUrl.length > 30
                ? currentBackend.baseUrl.substring(0, 30) + '...'
                : currentBackend.baseUrl}
            </code>
          </span>
        )}
        <Button size="small" icon={<SettingOutlined />} onClick={handleOpenAddModal}>
          管理后端
        </Button>
      </div>

      <Modal
        title={editingConfig ? '编辑后端配置' : '添加后端配置'}
        open={configModalVisible}
        onOk={handleSaveConfig}
        onCancel={() => {
          setConfigModalVisible(false)
          setEditingConfig(null)
          form.resetFields()
        }}
        okText="保存"
        cancelText="取消"
        width={600}
      >
        <Form form={form} layout="vertical" style={{ marginTop: 16 }}>
          <Form.Item 
            name="name" 
            label="后端名称" 
            rules={[{ required: true, whitespace: true, message: '请输入后端名称' }]}
          >
            <Input placeholder="例如: Pro 服务器、本地开发" onPressEnter={handleSaveConfig} />
          </Form.Item>
          <Form.Item
            name="baseUrl"
            label="后端服务地址（API Base URL）"
            rules={[{ required: true, whitespace: true, message: '请输入后端服务地址' }]}
            extra="完整的 API 基础 URL，如 http://host:port/api"
          >
            <Input placeholder="例如: http://21.6.243.90:8083/api" onPressEnter={handleSaveConfig} />
          </Form.Item>
          <Form.Item 
            name="apiKey" 
            label="API Key" 
            rules={[{ required: true, whitespace: true, message: '请输入 API Key' }]}
            extra="Tunely Server 的管理 API Key"
          >
            <Input.Password placeholder="输入 API Key（用于访问受保护的 API）" onPressEnter={handleSaveConfig} />
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}
