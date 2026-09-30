//! tunely CLI — 与 typescript/src/cli.ts 的旗标保持一致
//!
//! connect 的 --token/--server/--target 可省略，取值优先级：
//! CLI 参数 > 环境变量（TUNELY_TOKEN / TUNELY_SERVER / TUNELY_TARGET）> 配置文件 > 内置默认。
//! 运行期间把连接状态写入状态文件，供 `tunely status` 查询。

use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use clap::Parser;
use tunely::client::{TunnelClient, TunnelClientConfig};
use tunely::config::{self, CliOverrides};
use tunely::status::{self, RunState, StatusData};

#[derive(Parser)]
#[command(
    name = "tunely",
    version,
    about = "WebSocket Tunnel Client - 让内网服务可被外网访问（Rust 版）"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(clap::Subcommand)]
enum Command {
    /// 连接到隧道服务器
    Connect {
        /// 隧道令牌（也可用环境变量 TUNELY_TOKEN 或配置文件提供）
        #[arg(short = 't', long = "token")]
        token: Option<String>,
        /// 服务端 WebSocket URL（默认 ws://localhost:8000/ws/tunnel）
        #[arg(short = 's', long = "server")]
        server: Option<String>,
        /// 本地目标服务 URL（默认 http://localhost:8080）
        #[arg(short = 'T', long = "target")]
        target: Option<String>,
        /// 重连间隔（秒）
        #[arg(short = 'r', long = "reconnect")]
        reconnect: Option<u64>,
        /// 最大重连次数（0 = 无限）
        #[arg(long = "max-reconnect")]
        max_reconnect: Option<u32>,
        /// 请求默认超时（秒）
        #[arg(long = "request-timeout")]
        request_timeout: Option<u64>,
        /// 强制抢占已有连接
        #[arg(short = 'f', long = "force", action = clap::ArgAction::SetTrue)]
        force: Option<bool>,
        /// keepalive 发 ping 周期（秒）
        #[arg(long = "keepalive-interval", default_value_t = 25)]
        keepalive_interval: u64,
        /// keepalive 判死超时（秒）
        #[arg(long = "keepalive-timeout", default_value_t = 45)]
        keepalive_timeout: u64,
        /// 配置文件路径（省略时依次尝试 ./tunely-client.toml 与 ~/.config/tunely/client.toml）
        #[arg(long = "config", value_name = "PATH")]
        config: Option<PathBuf>,
    },
    /// 查看运行中的 tunely 客户端状态
    Status,
}

/// 打印错误到 stderr 并以退出码 1 退出
fn fail(msg: &str) -> ! {
    eprintln!("tunely: {msg}");
    std::process::exit(1);
}

#[tokio::main]
async fn main() {
    let cli = Cli::parse();

    match cli.command {
        Command::Connect {
            token,
            server,
            target,
            reconnect,
            max_reconnect,
            request_timeout,
            force,
            keepalive_interval,
            keepalive_timeout,
            config: config_path,
        } => {
            run_connect(
                CliOverrides {
                    token,
                    server,
                    target,
                    reconnect,
                    max_reconnect,
                    request_timeout,
                    force,
                    keepalive_interval: Some(keepalive_interval),
                    keepalive_timeout: Some(keepalive_timeout),
                },
                config_path,
            )
            .await;
        }
        Command::Status => run_status(),
    }
}

async fn run_connect(cli: CliOverrides, config_path: Option<PathBuf>) {
    // 配置文件：显式指定必须存在；默认路径存在才读取，全部缺失则静默跳过
    let (file_cfg, used_path) = match &config_path {
        Some(path) => match config::load_config_file(path) {
            Ok(Some(cfg)) => (cfg, Some(path.clone())),
            Ok(None) => fail(&format!("配置文件不存在: {}", path.display())),
            Err(e) => fail(&e),
        },
        None => match config::load_first_config(&config::default_config_paths()) {
            Ok(found) => found,
            Err(e) => fail(&e),
        },
    };
    if let Some(path) = &used_path {
        println!("已加载配置文件: {}", path.display());
    }

    let settings_list = match config::resolve_all(&cli, &file_cfg, &config::real_env) {
        Ok(s) => s,
        Err(e) => fail(&e),
    };

    println!("tunely - WebSocket Tunnel Client (Rust)");
    println!("  服务端: {}", settings_list[0].server);
    let multi = settings_list.len() > 1;
    if multi {
        println!("  隧道 ({}):", settings_list.len());
        for s in &settings_list {
            println!(
                "    - {} -> {}",
                s.name.as_deref().unwrap_or("-"),
                s.target
            );
        }
    } else {
        println!("  目标: {}", settings_list[0].target);
        if settings_list[0].force {
            println!("  强制模式: 将抢占已有连接");
        }
    }
    println!();

    // 状态文件：在连接成功 / 断开进入重连 / 报错 / 停止 时更新；
    // 槽位按会话标签（name，单隧道为 "default"）键控
    let tracker = Arc::new(StatusTracker::new(
        status::default_state_path(),
        settings_list
            .iter()
            .map(|s| s.name.clone().unwrap_or_else(|| "default".into()))
            .collect(),
    ));
    tracker.mark_starting();

    let mut clients: Vec<Arc<TunnelClient>> = Vec::new();
    for settings in &settings_list {
        let label = settings.name.clone().unwrap_or_else(|| "default".into());
        let prefix = if multi {
            format!("[{label}] ")
        } else {
            String::new()
        };
        let client = TunnelClient::new(TunnelClientConfig {
            server_url: settings.server.clone(),
            token: settings.token.clone(),
            target_url: settings.target.clone(),
            reconnect_interval: Duration::from_secs(settings.reconnect),
            max_reconnect_attempts: settings.max_reconnect,
            request_timeout: Duration::from_secs(settings.request_timeout),
            force: settings.force,
            keepalive_interval: Duration::from_secs(settings.keepalive_interval),
            keepalive_timeout: Duration::from_secs(settings.keepalive_timeout),
            name: settings.name.clone(),
        });
        let client = Arc::new(client);
        {
            let tracker = tracker.clone();
            let label = label.clone();
            let prefix = prefix.clone();
            client.on_connect(move |domain| {
                println!("{prefix}✓ 已连接: domain={domain}");
                tracker.set_connected(&label, domain);
            });
        }
        {
            let tracker = tracker.clone();
            let label = label.clone();
            let prefix = prefix.clone();
            client.on_disconnect(move || {
                println!("{prefix}! 连接断开");
                tracker.set_reconnecting(&label);
            });
        }
        {
            let tracker = tracker.clone();
            let prefix = prefix.clone();
            client.on_error(move |msg| {
                eprintln!("{prefix}✗ 错误: {msg}");
                tracker.set_error(&label, msg);
            });
        }
        clients.push(client);
    }

    {
        let clients = clients.clone();
        tokio::spawn(async move {
            let _ = tokio::signal::ctrl_c().await;
            println!("\n收到 Ctrl-C，正在停止…");
            for c in &clients {
                c.stop();
            }
            // 不在此处 process::exit：让 run() 自然返回后统一写最终状态
        });
    }

    // 每条隧道一个独立任务：会话间重连/退避互不影响；
    // 全部退出（stop 或耗尽 max_reconnect）后进程才结束
    let handles: Vec<_> = clients
        .into_iter()
        .map(|c| tokio::spawn(async move { c.run().await }))
        .collect();
    for handle in handles {
        let _ = handle.await;
    }
    tracker.set_stopped();
}

fn run_status() {
    let Some(path) = status::default_state_path() else {
        fail("not running");
    };
    match status::read_status(&path) {
        Ok(data) => print_status(&data),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => fail("not running"),
        Err(e) => fail(&format!("读取状态文件 {} 失败: {e}", path.display())),
    }
}

fn print_status(data: &StatusData) {
    println!("tunely 状态");
    println!("  pid:             {}", data.pid);
    println!("  state:           {}", data.state.as_str());
    println!(
        "  domain:          {}",
        data.domain.as_deref().unwrap_or("-")
    );
    println!("  reconnect_count: {}", data.reconnect_count);
    println!(
        "  last_error:      {}",
        data.last_error.as_deref().unwrap_or("-")
    );
    println!("  updated_at:      {}", data.updated_at);
    if let Some(tunnels) = &data.tunnels {
        println!("  tunnels ({}):", tunnels.len());
        for t in tunnels {
            println!(
                "    - {:<10} {:<12} domain={:<20} reconnect_count={} last_error={}",
                t.name,
                t.state.as_str(),
                t.domain.as_deref().unwrap_or("-"),
                t.reconnect_count,
                t.last_error.as_deref().unwrap_or("-"),
            );
        }
    }
}

/// 状态文件写入器：聚合当前连接状态，在状态变化点落盘。
/// 多隧道模式下每条隧道一个槽位（按配置 name 键控），状态文件带 tunnels 数组；
/// 单隧道模式保持 0.3.x 的单状态形状（tunnels=None 不落盘）。
struct StatusTracker {
    path: Option<PathBuf>,
    pid: u32,
    inner: Mutex<Inner>,
}

struct Slot {
    name: String,
    domain: Option<String>,
    state: RunState,
    reconnect_count: u32,
    last_error: Option<String>,
}

struct Inner {
    slots: Vec<Slot>,
    stopped: bool,
}

impl StatusTracker {
    fn new(path: Option<PathBuf>, names: Vec<String>) -> Self {
        if path.is_none() {
            eprintln!("警告: 未设置 TUNELY_STATE_FILE 且无法确定 HOME，状态文件功能不可用");
        }
        Self {
            path,
            pid: std::process::id(),
            inner: Mutex::new(Inner {
                slots: names
                    .into_iter()
                    .map(|name| Slot {
                        name,
                        domain: None,
                        state: RunState::Reconnecting,
                        reconnect_count: 0,
                        last_error: None,
                    })
                    .collect(),
                stopped: false,
            }),
        }
    }

    /// 启动即写一次（state=reconnecting），让首次连接尝试期间 `status` 也有内容
    fn mark_starting(&self) {
        self.flush();
    }

    fn set_connected(&self, name: &str, domain: &str) {
        let mut g = self.inner.lock().unwrap();
        if let Some(slot) = g.slots.iter_mut().find(|s| s.name == name) {
            slot.state = RunState::Connected;
            slot.domain = Some(domain.to_string());
            slot.reconnect_count = 0;
            slot.last_error = None;
        }
        drop(g);
        self.flush();
    }

    fn set_reconnecting(&self, name: &str) {
        let mut g = self.inner.lock().unwrap();
        if let Some(slot) = g.slots.iter_mut().find(|s| s.name == name) {
            slot.state = RunState::Reconnecting;
        }
        drop(g);
        self.flush();
    }

    fn set_error(&self, name: &str, msg: &str) {
        let mut g = self.inner.lock().unwrap();
        if let Some(slot) = g.slots.iter_mut().find(|s| s.name == name) {
            slot.state = RunState::Reconnecting;
            slot.last_error = Some(msg.to_string());
            slot.reconnect_count += 1;
        }
        drop(g);
        self.flush();
    }

    fn set_stopped(&self) {
        let mut g = self.inner.lock().unwrap();
        g.stopped = true;
        for slot in &mut g.slots {
            slot.state = RunState::Disconnected;
        }
        drop(g);
        self.flush();
    }

    fn flush(&self) {
        let Some(path) = &self.path else { return };
        let data = {
            let g = self.inner.lock().unwrap();
            // 聚合口径：任一隧道 Connected 即 Connected；全停才 Disconnected
            let state = if g.stopped {
                RunState::Disconnected
            } else if g.slots.iter().any(|s| s.state == RunState::Connected) {
                RunState::Connected
            } else {
                RunState::Reconnecting
            };
            StatusData {
                pid: self.pid,
                state,
                domain: g.slots.iter().find_map(|s| s.domain.clone()),
                reconnect_count: g.slots.iter().map(|s| s.reconnect_count).sum(),
                last_error: g.slots.iter().find_map(|s| s.last_error.clone()),
                updated_at: status::now_rfc3339(),
                tunnels: (g.slots.len() > 1).then(|| {
                    g.slots
                        .iter()
                        .map(|s| status::TunnelStatus {
                            name: s.name.clone(),
                            state: s.state,
                            domain: s.domain.clone(),
                            reconnect_count: s.reconnect_count,
                            last_error: s.last_error.clone(),
                        })
                        .collect()
                }),
            }
        };
        if let Err(e) = status::write_status(path, &data) {
            eprintln!("警告: 写状态文件失败: {e}");
        }
    }
}
