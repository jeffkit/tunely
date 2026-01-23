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

program
  .command('connect')
  .description('连接到隧道服务器')
  .requiredOption('-t, --token <token>', '隧道令牌')
  .option('-s, --server <url>', '服务端 WebSocket URL', 'ws://localhost:8000/ws/tunnel')
  .option('-T, --target <url>', '本地目标服务 URL', 'http://localhost:8080')
  .option('-r, --reconnect <seconds>', '重连间隔（秒）', '5')
  .option('-f, --force', '强制抢占已有连接', false)
  .action(async (options) => {
    console.log('tunely - WebSocket Tunnel Client');
    console.log(`  服务端: ${options.server}`);
    console.log(`  目标: ${options.target}`);
    if (options.force) {
      console.log('  强制模式: 将抢占已有连接');
    }
    console.log();

    const client = new TunnelClient({
      serverUrl: options.server,
      token: options.token,
      targetUrl: options.target,
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
