/**
 * 二维码渲染封装
 *
 * 内容：契约 §6 要求「GET /api/console/entry 返回的 qr JSON 原样编码」，
 * 调用方传入 JSON.stringify(entry.qr) 的结果，本模块不做任何改写。
 *
 * 渲染：引入 qrcode 库（契约点名的轻量选择）输出 SVG 字符串：
 * - QR 编码含 Reed-Solomon 纠错、掩码评分与分版本分块表，手写实现极易出错
 *   且错误不可见（生成成功但扫不出来），不宜自绘；
 * - qrcode 无 React 包装、支持纯字符串 SVG 输出（不依赖 canvas，jsdom 可测），体积小。
 */
import QRCode from 'qrcode'

/** 生成二维码 SVG 字符串（失败时抛错，由调用方提示） */
export async function renderQrSvg(content: string): Promise<string> {
  return QRCode.toString(content, {
    type: 'svg',
    errorCorrectionLevel: 'M',
    margin: 1,
    width: 224,
  })
}
