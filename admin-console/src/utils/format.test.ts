/**
 * 格式化工具函数单元测试
 */
import { describe, it, expect } from 'vitest'
import { formatDate, formatRelativeTime, formatNumber, formatDateTime, formatDuration } from './format'

describe('format utils', () => {
  describe('formatDate', () => {
    it('should format valid date strings', () => {
      const result = formatDate('2024-01-15T10:30:00')
      expect(result).toMatch(/2024-01-15 \d{2}:\d{2}:\d{2}/)
    })

    it('should return "-" for null', () => {
      expect(formatDate(null)).toBe('-')
    })

    it('should return "-" for empty string', () => {
      expect(formatDate('')).toBe('-')
    })
  })

  describe('formatDateTime', () => {
    it('should format valid datetime strings', () => {
      const result = formatDateTime('2024-01-15T10:30:00')
      expect(result).toMatch(/2024-01-15 \d{2}:\d{2}:\d{2}/)
    })

    it('should return "-" for null', () => {
      expect(formatDateTime(null)).toBe('-')
    })
  })

  describe('formatRelativeTime', () => {
    it('should return "-" for null', () => {
      expect(formatRelativeTime(null)).toBe('-')
    })

    it('should format recent dates', () => {
      const now = new Date()
      const result = formatRelativeTime(now.toISOString())
      expect(result).toContain('秒前')
    })
  })

  describe('formatNumber', () => {
    it('should format numbers with thousands separator', () => {
      expect(formatNumber(1000)).toBe('1,000')
      expect(formatNumber(1234567)).toBe('1,234,567')
    })

    it('should handle small numbers', () => {
      expect(formatNumber(0)).toBe('0')
      expect(formatNumber(123)).toBe('123')
    })

    it('should handle negative numbers', () => {
      expect(formatNumber(-1000)).toBe('-1,000')
    })
  })

  describe('formatDuration', () => {
    it('should format milliseconds', () => {
      expect(formatDuration(500)).toBe('500ms')
      expect(formatDuration(999)).toBe('999ms')
    })

    it('should format seconds', () => {
      expect(formatDuration(1000)).toBe('1.00s')
      expect(formatDuration(5500)).toBe('5.50s')
    })

    it('should format minutes and seconds', () => {
      expect(formatDuration(60000)).toBe('1m 0.00s')
      expect(formatDuration(90000)).toBe('1m 30.00s')
      expect(formatDuration(125000)).toBe('2m 5.00s')
    })

    it('should handle zero', () => {
      expect(formatDuration(0)).toBe('0ms')
    })
  })
})
