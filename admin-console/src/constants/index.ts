/**
 * 应用常量配置
 */

// API 配置
export const API_CONFIG = {
  /** 请求超时时间（毫秒） */
  TIMEOUT: 30000,
  /** 最大重试次数 */
  MAX_RETRIES: 3,
}

// 轮询配置
export const POLLING_CONFIG = {
  /** 默认轮询间隔（毫秒） */
  DEFAULT_INTERVAL: 5000,
  /** 活跃时轮询间隔（毫秒） */
  ACTIVE_INTERVAL: 3000,
  /** 不活跃时轮询间隔（毫秒） */
  INACTIVE_INTERVAL: 10000,
}

// 分页配置
export const PAGINATION_CONFIG = {
  /** 默认每页条数 */
  DEFAULT_PAGE_SIZE: 50,
  /** 每页条数选项 */
  PAGE_SIZE_OPTIONS: [20, 50, 100] as const,
  /** 隧道列表每页条数 */
  TUNNEL_PAGE_SIZE: 20,
}

// UI 配置
export const UI_CONFIG = {
  /** 表格骨架屏行数 */
  SKELETON_ROWS: 5,
  /** 表格骨架屏列数 */
  SKELETON_COLUMNS: 6,
  /** Modal 最大宽度 */
  MODAL_MAX_WIDTH: 900,
  /** 大 Modal 宽度（百分比） */
  MODAL_LARGE_WIDTH: '90%',
}

// 时间配置
export const TIME_CONFIG = {
  /** 时间格式 */
  DATETIME_FORMAT: 'YYYY-MM-DD HH:mm:ss',
  /** 日期格式 */
  DATE_FORMAT: 'YYYY-MM-DD',
}
