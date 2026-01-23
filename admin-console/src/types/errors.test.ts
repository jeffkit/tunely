/**
 * 错误类型单元测试
 */
import { describe, it, expect } from 'vitest'
import { ApiError, NetworkError, TimeoutError } from './errors'

describe('error types', () => {
  describe('ApiError', () => {
    it('should create ApiError with message and status code', () => {
      const error = new ApiError('Test error', 400)
      expect(error.name).toBe('ApiError')
      expect(error.message).toBe('Test error')
      expect(error.statusCode).toBe(400)
      expect(error.detail).toBe('Test error')
    })

    it('should create ApiError from axios error', () => {
      const axiosError = {
        response: {
          status: 404,
          data: { detail: 'Not found' },
        },
        message: 'Request failed',
      }
      const error = ApiError.fromAxiosError(axiosError)
      expect(error.statusCode).toBe(404)
      expect(error.detail).toBe('Not found')
    })

    it('should detect auth errors', () => {
      const error401 = new ApiError('Unauthorized', 401)
      const error403 = new ApiError('Forbidden', 403)
      const error404 = new ApiError('Not found', 404)

      expect(error401.isAuthError()).toBe(true)
      expect(error403.isAuthError()).toBe(true)
      expect(error404.isAuthError()).toBe(false)
    })

    it('should detect network errors', () => {
      const error500 = new ApiError('Server error', 500)
      const error503 = new ApiError('Service unavailable', 503)
      const error400 = new ApiError('Bad request', 400)

      expect(error500.isNetworkError()).toBe(true)
      expect(error503.isNetworkError()).toBe(true)
      expect(error400.isNetworkError()).toBe(false)
    })

    it('should detect client errors', () => {
      const error400 = new ApiError('Bad request', 400)
      const error404 = new ApiError('Not found', 404)
      const error500 = new ApiError('Server error', 500)

      expect(error400.isClientError()).toBe(true)
      expect(error404.isClientError()).toBe(true)
      expect(error500.isClientError()).toBe(false)
    })

    it('should provide user-friendly messages', () => {
      const error = new ApiError('请求参数错误: Invalid email', 400)
      expect(error.getUserMessage()).toBe('请求参数错误: Invalid email')
    })

    it('should provide detail messages', () => {
      const error = new ApiError('Test', 400, 'Detailed error info')
      expect(error.getDetailMessage()).toBe('Detailed error info')
    })

    it('should generate status-based messages', () => {
      const axiosError = {
        response: {
          status: 429,
        },
        message: 'Too many requests',
      }
      const error = ApiError.fromAxiosError(axiosError)
      expect(error.message).toContain('请求过于频繁')
    })
  })

  describe('NetworkError', () => {
    it('should create NetworkError', () => {
      const error = new NetworkError()
      expect(error.name).toBe('NetworkError')
      expect(error.message).toContain('网络连接失败')
      expect(error.statusCode).toBe(0)
    })

    it('should accept custom message', () => {
      const error = new NetworkError('Custom network error')
      expect(error.message).toBe('Custom network error')
    })
  })

  describe('TimeoutError', () => {
    it('should create TimeoutError', () => {
      const error = new TimeoutError()
      expect(error.name).toBe('TimeoutError')
      expect(error.message).toContain('请求超时')
      expect(error.statusCode).toBe(0)
    })

    it('should accept custom message', () => {
      const error = new TimeoutError('Custom timeout error')
      expect(error.message).toBe('Custom timeout error')
    })
  })
})
