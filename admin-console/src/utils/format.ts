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
  return num.toLocaleString('zh-CN')
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
