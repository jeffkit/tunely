#!/usr/bin/env node

/**
 * tunely CLI
 *
 * 命令行工具，用于启动隧道客户端
 */

import { Command } from 'commander';
import { TunnelClient } from './client.js';

const program = new Command();

program
  .name('tunely')
  .description('WebSocket Tunnel Client - 让内网服务可被外网访问')
  .version('0.1.0');

const DEFAULT_SERVER = 'ws://localhost:8000/ws/tunnel';
const DEFAULT_TARGET = 'http://localhost:8080';

program
  .command('connect')
  .description('连接到隧道服务器')
  .option('-t, --token <token>', '隧道令牌（也可通过环境变量 TUNELY_TOKEN 提供）')
  .option('-s, --server <url>', `服务端 WebSocket URL（也可通过环境变量 TUNELY_SERVER 提供，默认 ${DEFAULT_SERVER}）`)
  .option('-T, --target <url>', `本地目标服务 URL（也可通过环境变量 TUNELY_TARGET 提供，默认 ${DEFAULT_TARGET}）`)
  .option('-r, --reconnect <seconds>', '重连间隔（秒）', '5')
  .option('-f, --force', '强制抢占已有连接', false)
  .action(async (options) => {
    // 取值优先级：命令行参数 > 环境变量 > 默认值
    const token = options.token ?? process.env.TUNELY_TOKEN;
    const server = options.server ?? process.env.TUNELY_SERVER ?? DEFAULT_SERVER;
    const target = options.target ?? process.env.TUNELY_TARGET ?? DEFAULT_TARGET;

    if (!token) {
      console.error('错误: 缺少隧道令牌，请通过 --token 参数或环境变量 TUNELY_TOKEN 提供');
      process.exit(1);
    }

    console.log('tunely - WebSocket Tunnel Client');
    console.log(`  服务端: ${server}`);
    console.log(`  目标: ${target}`);
    if (options.force) {
      console.log('  强制模式: 将抢占已有连接');
    }
    console.log();

    const client = new TunnelClient({
      serverUrl: server,
      token,
      targetUrl: target,
      reconnectInterval: parseFloat(options.reconnect) * 1000,
      force: options.force,
    });

    client.on('onConnect', (domain) => {
      console.log(`✓ 已连接: domain=${domain}`);
    });

    client.on('onDisconnect', () => {
      console.log('! 连接断开');
    });

    client.on('onError', (error) => {
      console.error('✗ 错误:', error.message);
    });

    // 处理 Ctrl+C
    process.on('SIGINT', () => {
      console.log('\n已停止');
      client.stop();
      process.exit(0);
    });

    await client.run();
  });

program.parse();
