import { useState, useEffect } from 'react'
import { Table, Card, Tag, Button, Space, Descriptions, Modal, message, Select } from 'antd'
import { ReloadOutlined, EyeOutlined } from '@ant-design/icons'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneLight } from 'react-syntax-highlighter/dist/esm/styles/prism'
import { api } from '../api/client'
import type { TunnelRequestLog } from '../types'
import { ApiError } from '../types/errors'
import { formatDateTime, formatDuration } from '../utils/format'
import { formatJsonString, isJsonString } from '../utils/json'
import { useTunnels } from '../hooks/useTunnels'
import { PAGINATION_CONFIG, UI_CONFIG } from '../constants'

interface RequestLogsProps {
  tunnelDomain: string | null
}

export function RequestLogs({ tunnelDomain: initialTunnelDomain }: RequestLogsProps) {
  const { tunnels } = useTunnels()
  const [tunnelDomain, setTunnelDomain] = useState<string | null>(initialTunnelDomain)
  const [logs, setLogs] = useState<TunnelRequestLog[]>([])
  const [loading, setLoading] = useState(false)
  const [total, setTotal] = useState(0)
  const [selectedLog, setSelectedLog] = useState<TunnelRequestLog | null>(null)
  const [detailVisible, setDetailVisible] = useState(false)
  const [pagination, setPagination] = useState({
    current: 1,
    pageSize: PAGINATION_CONFIG.DEFAULT_PAGE_SIZE,
  })

  useEffect(() => {
    setTunnelDomain(initialTunnelDomain)
  }, [initialTunnelDomain])

  const loadLogs = async () => {
    if (!tunnelDomain) {
      setLogs([])
      setTotal(0)
      return
    }

    setLoading(true)
    try {
      const offset = (pagination.current - 1) * pagination.pageSize
      const response = await api.getTunnelLogs(
        tunnelDomain,
        pagination.pageSize,
        offset
      )
      setLogs(response.logs)
      setTotal(response.total)
    } catch (err) {
      const error = err instanceof ApiError ? err : new ApiError('加载失败', 500, String(err))
      message.error(error.getUserMessage())
      setLogs([])
      setTotal(0)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadLogs()
  }, [tunnelDomain, pagination.current, pagination.pageSize])

  const getStatusColor = (statusCode: number | null) => {
    if (!statusCode) return 'default'
    if (statusCode >= 200 && statusCode < 300) return 'success'
    if (statusCode >= 300 && statusCode < 400) return 'warning'
    if (statusCode >= 400 && statusCode < 500) return 'error'
    if (statusCode >= 500) return 'error'
    return 'default'
  }

  const getMethodColor = (method: string) => {
    const colors: Record<string, string> = {
      GET: 'blue',
      POST: 'green',
      PUT: 'orange',
      DELETE: 'red',
      PATCH: 'purple',
    }
    return colors[method] || 'default'
  }

  const columns = [
    {
      title: '时间',
      dataIndex: 'timestamp',
      key: 'timestamp',
      width: 180,
      render: (text: string) => formatDateTime(text),
    },
    {
      title: '方法',
      dataIndex: 'method',
      key: 'method',
      width: 80,
      render: (method: string) => (
        <Tag color={getMethodColor(method)}>{method}</Tag>
      ),
    },
    {
      title: '路径',
      dataIndex: 'path',
      key: 'path',
      ellipsis: true,
    },
    {
      title: '状态',
      dataIndex: 'status_code',
      key: 'status_code',
      width: 100,
      render: (statusCode: number | null) => {
        if (statusCode === null) {
          return <Tag color="default">-</Tag>
        }
        return (
          <Tag color={getStatusColor(statusCode)}>
            {statusCode}
          </Tag>
        )
      },
    },
    {
      title: '耗时',
      dataIndex: 'duration_ms',
      key: 'duration_ms',
      width: 100,
      render: (ms: number) => formatDuration(ms),
    },
    {
      title: '错误',
      dataIndex: 'error',
      key: 'error',
      width: 150,
      ellipsis: true,
      render: (error: string | null) =>
        error ? <Tag color="error">{error.substring(0, 50)}</Tag> : '-',
    },
    {
      title: '操作',
      key: 'action',
      width: 100,
      render: (_: any, record: TunnelRequestLog) => (
        <Button
          type="link"
          icon={<EyeOutlined />}
          onClick={() => {
            setSelectedLog(record)
            setDetailVisible(true)
          }}
        >
          详情
        </Button>
      ),
    },
  ]

  return (
    <div>
      <Card
        title={
          <Space>
            <span>请求历史</span>
            <Select
              placeholder="选择隧道"
              value={tunnelDomain}
              onChange={(value) => {
                setTunnelDomain(value)
                setPagination({ current: 1, pageSize: PAGINATION_CONFIG.DEFAULT_PAGE_SIZE })
              }}
              style={{ width: 200 }}
              showSearch
              filterOption={(input, option) =>
                (option?.label ?? '').toLowerCase().includes(input.toLowerCase())
              }
              options={tunnels.map(t => ({
                label: t.name || t.domain,
                value: t.domain,
              }))}
            />
            {tunnelDomain && (
              <>
                <Tag color="blue">{tunnelDomain}</Tag>
                <Button
                  icon={<ReloadOutlined />}
                  onClick={loadLogs}
                  loading={loading}
                >
                  刷新
                </Button>
              </>
            )}
          </Space>
        }
      >
        {!tunnelDomain ? (
          <div style={{ textAlign: 'center', padding: '40px', color: '#999' }}>
            请在上方选择一个隧道查看请求历史
          </div>
        ) : (
          <Table
            columns={columns}
            dataSource={logs}
            loading={loading}
            rowKey="id"
            pagination={{
              current: pagination.current,
              pageSize: pagination.pageSize,
              total: total,
              showSizeChanger: true,
              showTotal: (total) => `共 ${total} 条记录`,
              onChange: (page, pageSize) => {
                setPagination({
                  current: page,
                  pageSize: pageSize || PAGINATION_CONFIG.DEFAULT_PAGE_SIZE,
                })
              },
            }}
          />
        )}
      </Card>

      <Modal
        title="请求详情"
        open={detailVisible}
        onCancel={() => setDetailVisible(false)}
        footer={null}
        width={UI_CONFIG.MODAL_MAX_WIDTH}
      >
        {selectedLog && (
          <>
            {/* 紧凑的基础信息区域 */}
            <Descriptions column={2} bordered size="small" style={{ marginBottom: 16 }}>
              <Descriptions.Item label="时间" span={1}>
                {formatDateTime(selectedLog.timestamp)}
              </Descriptions.Item>
              <Descriptions.Item label="隧道域名" span={1}>
                {selectedLog.tunnel_domain}
              </Descriptions.Item>
              <Descriptions.Item label="方法" span={1}>
                <Tag color={getMethodColor(selectedLog.method)}>
                  {selectedLog.method}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="路径" span={1}>
                <code style={{ fontSize: '12px' }}>{selectedLog.path}</code>
              </Descriptions.Item>
              <Descriptions.Item label="状态码" span={1}>
                {selectedLog.status_code ? (
                  <Tag color={getStatusColor(selectedLog.status_code)}>
                    {selectedLog.status_code}
                  </Tag>
                ) : (
                  '-'
                )}
              </Descriptions.Item>
              <Descriptions.Item label="耗时" span={1}>
                {formatDuration(selectedLog.duration_ms)}
              </Descriptions.Item>
              {selectedLog.error && (
                <Descriptions.Item label="错误信息" span={2}>
                  <Tag color="error">{selectedLog.error}</Tag>
                </Descriptions.Item>
              )}
            </Descriptions>

            {/* 请求头 - 代码块显示 */}
            {selectedLog.request_headers && (
              <div style={{ marginBottom: 16 }}>
                <div style={{ marginBottom: 8, fontWeight: 500 }}>请求头</div>
                <div style={{ border: '1px solid #d9d9d9', borderRadius: '4px', overflow: 'hidden' }}>
                  <SyntaxHighlighter
                    language="json"
                    style={oneLight}
                    wrapLines={true}
                    wrapLongLines={true}
                    customStyle={{
                      margin: 0,
                      maxHeight: '500px',
                      overflow: 'auto',
                      fontSize: '13px',
                      padding: '12px',
                      backgroundColor: '#fafafa',
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-word',
                      overflowWrap: 'break-word',
                    }}
                  >
                    {JSON.stringify(selectedLog.request_headers, null, 2)}
                  </SyntaxHighlighter>
                </div>
              </div>
            )}

            {/* 请求体 - 代码块显示 */}
            {selectedLog.request_body && (
              <div style={{ marginBottom: 16 }}>
                <div style={{ marginBottom: 8, fontWeight: 500 }}>请求体</div>
                <div style={{ border: '1px solid #d9d9d9', borderRadius: '4px', overflow: 'hidden' }}>
                  <SyntaxHighlighter
                    language={isJsonString(selectedLog.request_body) ? 'json' : 'text'}
                    style={oneLight}
                    wrapLines={true}
                    wrapLongLines={true}
                    customStyle={{
                      margin: 0,
                      maxHeight: '500px',
                      overflow: 'auto',
                      fontSize: '13px',
                      padding: '12px',
                      backgroundColor: '#fafafa',
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-word',
                      overflowWrap: 'break-word',
                    }}
                  >
                    {formatJsonString(selectedLog.request_body)}
                  </SyntaxHighlighter>
                </div>
              </div>
            )}

            {/* 响应头 - 代码块显示 */}
            {selectedLog.response_headers && (
              <div style={{ marginBottom: 16 }}>
                <div style={{ marginBottom: 8, fontWeight: 500 }}>响应头</div>
                <div style={{ border: '1px solid #d9d9d9', borderRadius: '4px', overflow: 'hidden' }}>
                  <SyntaxHighlighter
                    language="json"
                    style={oneLight}
                    wrapLines={true}
                    wrapLongLines={true}
                    customStyle={{
                      margin: 0,
                      maxHeight: '500px',
                      overflow: 'auto',
                      fontSize: '13px',
                      padding: '12px',
                      backgroundColor: '#fafafa',
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-word',
                      overflowWrap: 'break-word',
                    }}
                  >
                    {JSON.stringify(selectedLog.response_headers, null, 2)}
                  </SyntaxHighlighter>
                </div>
              </div>
            )}

            {/* 响应体 - 代码块显示 */}
            {selectedLog.response_body && (() => {
              // 先尝试格式化，如果失败则使用原始内容
              let formattedBody: string
              let bodyIsJson: boolean
              
              try {
                formattedBody = formatJsonString(selectedLog.response_body)
                bodyIsJson = isJsonString(selectedLog.response_body) || 
                           (formattedBody.trim().startsWith('{') || formattedBody.trim().startsWith('['))
              } catch {
                formattedBody = selectedLog.response_body
                bodyIsJson = false
              }
              
              return (
                <div style={{ marginBottom: 16 }}>
                  <div style={{ marginBottom: 8, fontWeight: 500 }}>响应体</div>
                  <div style={{ border: '1px solid #d9d9d9', borderRadius: '4px', overflow: 'hidden' }}>
                    <SyntaxHighlighter
                      language={bodyIsJson ? 'json' : 'text'}
                      style={oneLight}
                      wrapLines={true}
                      wrapLongLines={true}
                      customStyle={{
                        margin: 0,
                        maxHeight: '500px',
                        overflow: 'auto',
                        fontSize: '13px',
                        padding: '12px',
                        backgroundColor: '#fafafa',
                        whiteSpace: 'pre-wrap',
                        wordBreak: 'break-word',
                        overflowWrap: 'break-word',
                      }}
                    >
                      {formattedBody}
                    </SyntaxHighlighter>
                  </div>
                </div>
              )
            })()}
          </>
        )}
      </Modal>
    </div>
  )
}
