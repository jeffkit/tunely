/**
 * 客户端版本标识，随 package.json 版本发布时同步更新。
 *
 * AuthMessage.client_version 会上报到服务端（/api/tunnels 可查），
 * 用于升级前核对现网客户端版本分布——历史版本曾硬编码 '0.1.0'（假值），
 * 0.2.7 起上报真实版本。防漂移测试见 protocol.conformance.test.ts。
 */
export const CLIENT_VERSION = '0.4.0';
