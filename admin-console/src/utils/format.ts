import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'
import utc from 'dayjs/plugin/utc'
import timezone from 'dayjs/plugin/timezone'
import zhCN from 'dayjs/locale/zh-cn'

dayjs.extend(relativeTime)
dayjs.extend(utc)
dayjs.extend(timezone)
dayjs.locale(zhCN)

// 设置默认时区为本地时区
dayjs.tz.setDefault(dayjs.tz.guess())

export function formatDate(date: string | null): string {
  if (!date) return '-'
  // 将 UTC 时间转换为本地时区
  return dayjs.utc(date).local().format('YYYY-MM-DD HH:mm:ss')
}

export function formatRelativeTime(date: string | null): string {
  if (!date) return '-'
  // 将 UTC 时间转换为本地时区
  return dayjs.utc(date).local().fromNow()
}

export function formatNumber(num: number): string {
  if (!Number.isFinite(num)) return '-'
  return num.toLocaleString('zh-CN')
}

/**
 * 人性化格式化字节数（B/KB/MB/GB/TB）
 * 字段缺失或非法值返回 '-'，0 显示 '0 B'
 */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes) || bytes < 0) {
    return '-'
  }
  if (bytes === 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let value = bytes
  let unitIndex = 0
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024
    unitIndex++
  }
  const formatted = unitIndex === 0 ? String(value) : value.toFixed(2)
  return `${formatted} ${units[unitIndex]}`
}

export function formatDateTime(date: string | null): string {
  if (!date) return '-'
  // 将 UTC 时间转换为本地时区
  return dayjs.utc(date).local().format('YYYY-MM-DD HH:mm:ss')
}

export function formatDuration(ms: number): string {
  if (ms < 1000) {
    return `${ms}ms`
  }
  if (ms < 60000) {
    return `${(ms / 1000).toFixed(2)}s`
  }
  const minutes = Math.floor(ms / 60000)
  const seconds = ((ms % 60000) / 1000).toFixed(2)
  return `${minutes}m ${seconds}s`
}
