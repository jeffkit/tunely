/**
 * API 错误类型定义
 */

export interface ApiErrorDetail {
  detail?: string
  message?: string
  code?: string
  field?: string
}

export class ApiError extends Error {
  public readonly statusCode: number
  public readonly detail: string
  public readonly originalError: any

  constructor(message: string, statusCode: number, detail?: string, originalError?: any) {
    super(message)
    this.name = 'ApiError'
    this.statusCode = statusCode
    this.detail = detail || message
    this.originalError = originalError

    // 维持正确的原型链
    Object.setPrototypeOf(this, ApiError.prototype)
  }

  /**
   * 从 axios 错误创建 ApiError
   */
  static fromAxiosError(error: any): ApiError {
    const statusCode = error.response?.status || 500
    const detail = error.response?.data?.detail || error.response?.data?.message || error.message
    const message = this.getMessageByStatusCode(statusCode, detail)

    return new ApiError(message, statusCode, detail, error)
  }

  /**
   * 根据状态码生成友好的错误消息
   */
  private static getMessageByStatusCode(statusCode: number, detail?: string): string {
    const defaultMessages: Record<number, string> = {
      400: '请求参数错误',
      401: 'API Key 无效或已过期',
      403: '没有权限执行此操作',
      404: '资源不存在',
      409: '资源已存在或冲突',
      422: '请求数据验证失败',
      429: '请求过于频繁，请稍后再试',
      500: '服务器内部错误',
      502: '网关错误',
      503: '服务暂时不可用',
      504: '网关超时',
    }

    const baseMessage = defaultMessages[statusCode] || '请求失败'
    return detail ? `${baseMessage}: ${detail}` : baseMessage
  }

  /**
   * 判断是否为认证错误
   */
  isAuthError(): boolean {
    return this.statusCode === 401 || this.statusCode === 403
  }

  /**
   * 判断是否为网络错误
   */
  isNetworkError(): boolean {
    return this.statusCode >= 500 || this.statusCode === 0
  }

  /**
   * 判断是否为客户端错误
   */
  isClientError(): boolean {
    return this.statusCode >= 400 && this.statusCode < 500
  }

  /**
   * 获取用户友好的错误消息
   */
  getUserMessage(): string {
    return this.message
  }

  /**
   * 获取详细错误信息（用于调试）
   */
  getDetailMessage(): string {
    return this.detail
  }
}

/**
 * 网络错误
 */
export class NetworkError extends ApiError {
  constructor(message: string = '网络连接失败，请检查网络设置', originalError?: any) {
    super(message, 0, message, originalError)
    this.name = 'NetworkError'
    Object.setPrototypeOf(this, NetworkError.prototype)
  }
}

/**
 * 超时错误
 */
export class TimeoutError extends ApiError {
  constructor(message: string = '请求超时，请稍后重试', originalError?: any) {
    super(message, 0, message, originalError)
    this.name = 'TimeoutError'
    Object.setPrototypeOf(this, TimeoutError.prototype)
  }
}
