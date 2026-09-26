//! 配置解析与合并：CLI 参数 > 环境变量（TUNELY_TOKEN / TUNELY_SERVER / TUNELY_TARGET）
//! > 配置文件（TOML）> 内置默认。
//!
//! 环境变量与 HOME 的读取以闭包/参数注入纯函数，单测不触碰进程全局状态。

use std::path::{Path, PathBuf};

use serde::Deserialize;

/// 内置默认（与 TS 客户端 CLI 默认一致）
pub const DEFAULT_SERVER: &str = "ws://localhost:8000/ws/tunnel";
pub const DEFAULT_TARGET: &str = "http://localhost:8080";

pub const ENV_TOKEN: &str = "TUNELY_TOKEN";
pub const ENV_SERVER: &str = "TUNELY_SERVER";
pub const ENV_TARGET: &str = "TUNELY_TARGET";

/// 配置文件（TOML）字段，全部可选；未知字段忽略
#[derive(Debug, Clone, Default, Deserialize)]
pub struct FileConfig {
    pub server: Option<String>,
    pub token: Option<String>,
    pub target: Option<String>,
    pub reconnect_secs: Option<u64>,
    pub max_reconnect: Option<u32>,
    pub request_timeout_secs: Option<u64>,
    pub force: Option<bool>,
    pub keepalive_interval_secs: Option<u64>,
    pub keepalive_timeout_secs: Option<u64>,
}

/// CLI 侧可覆盖项（None = 未提供；数值旗标不再用 clap 默认值，才能区分「没传」与「传了默认值」）
#[derive(Debug, Clone, Default)]
pub struct CliOverrides {
    pub token: Option<String>,
    pub server: Option<String>,
    pub target: Option<String>,
    pub reconnect: Option<u64>,
    pub max_reconnect: Option<u32>,
    pub request_timeout: Option<u64>,
    pub force: Option<bool>,
    pub keepalive_interval: Option<u64>,
    pub keepalive_timeout: Option<u64>,
}

/// 合并后的最终配置
#[derive(Debug, Clone)]
pub struct Settings {
    pub token: String,
    pub server: String,
    pub target: String,
    pub reconnect: u64,
    pub max_reconnect: u32,
    pub request_timeout: u64,
    pub force: bool,
    pub keepalive_interval: u64,
    pub keepalive_timeout: u64,
}

/// 按 CLI > env > file > 默认 的优先级合并出最终配置；
/// 三处都拿不到 token 时返回 Err。
pub fn resolve(
    cli: &CliOverrides,
    file: &FileConfig,
    env: &dyn Fn(&str) -> Option<String>,
) -> Result<Settings, String> {
    let token = cli
        .token
        .clone()
        .or_else(|| env(ENV_TOKEN))
        .or_else(|| file.token.clone())
        .ok_or_else(|| {
            format!("缺少 token：请通过 --token、环境变量 {ENV_TOKEN} 或配置文件提供")
        })?;
    let server = cli
        .server
        .clone()
        .or_else(|| env(ENV_SERVER))
        .or_else(|| file.server.clone())
        .unwrap_or_else(|| DEFAULT_SERVER.to_string());
    let target = cli
        .target
        .clone()
        .or_else(|| env(ENV_TARGET))
        .or_else(|| file.target.clone())
        .unwrap_or_else(|| DEFAULT_TARGET.to_string());
    Ok(Settings {
        token,
        server,
        target,
        reconnect: cli.reconnect.or(file.reconnect_secs).unwrap_or(5),
        max_reconnect: cli.max_reconnect.or(file.max_reconnect).unwrap_or(0),
        request_timeout: cli
            .request_timeout
            .or(file.request_timeout_secs)
            .unwrap_or(300),
        force: cli.force.or(file.force).unwrap_or(false),
        keepalive_interval: cli
            .keepalive_interval
            .or(file.keepalive_interval_secs)
            .unwrap_or(25),
        keepalive_timeout: cli
            .keepalive_timeout
            .or(file.keepalive_timeout_secs)
            .unwrap_or(45),
    })
}

/// 真实环境变量查找：读取后去除首尾空白，空白串视为未设置
pub fn real_env(key: &str) -> Option<String> {
    std::env::var(key)
        .ok()
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
}

/// 当前用户 HOME 目录（$HOME，空白视为未设置）
pub fn home_dir() -> Option<String> {
    std::env::var("HOME")
        .ok()
        .map(|h| h.trim().to_string())
        .filter(|h| !h.is_empty())
}

/// 配置文件查找路径（按序）：./tunely-client.toml、$HOME/.config/tunely/client.toml
pub fn config_paths_with(home: Option<&str>) -> Vec<PathBuf> {
    let mut paths = vec![PathBuf::from("./tunely-client.toml")];
    if let Some(h) = home.filter(|h| !h.is_empty()) {
        paths.push(
            PathBuf::from(h)
                .join(".config")
                .join("tunely")
                .join("client.toml"),
        );
    }
    paths
}

/// 默认配置文件查找路径
pub fn default_config_paths() -> Vec<PathBuf> {
    config_paths_with(home_dir().as_deref())
}

/// 读取单个配置文件：不存在 → Ok(None)；存在但读取/解析失败 → Err（带文件路径上下文）
pub fn load_config_file(path: &Path) -> Result<Option<FileConfig>, String> {
    if !path.exists() {
        return Ok(None);
    }
    let text = std::fs::read_to_string(path)
        .map_err(|e| format!("读取配置文件 {} 失败: {e}", path.display()))?;
    let cfg: FileConfig =
        toml::from_str(&text).map_err(|e| format!("解析配置文件 {} 失败: {e}", path.display()))?;
    Ok(Some(cfg))
}

/// 依次尝试给定路径，返回第一个存在的配置；都不存在返回默认空配置。
/// 任一存在的文件解析失败则整体报错。
pub fn load_first_config(paths: &[PathBuf]) -> Result<(FileConfig, Option<PathBuf>), String> {
    for path in paths {
        if let Some(cfg) = load_config_file(path)? {
            return Ok((cfg, Some(path.clone())));
        }
    }
    Ok((FileConfig::default(), None))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 构造注入式环境变量查找闭包
    fn env<'a>(pairs: &'a [(&'a str, &'a str)]) -> impl Fn(&str) -> Option<String> + 'a {
        move |key: &str| {
            pairs
                .iter()
                .find(|(name, _)| *name == key)
                .map(|(_, v)| v.to_string())
        }
    }

    #[test]
    fn resolve_cli_wins_over_env_and_file() {
        let cli = CliOverrides {
            token: Some("t-cli".into()),
            server: Some("ws://cli".into()),
            target: Some("http://cli".into()),
            ..Default::default()
        };
        let file = FileConfig {
            token: Some("t-file".into()),
            server: Some("ws://file".into()),
            target: Some("http://file".into()),
            ..Default::default()
        };
        let e = env(&[
            (ENV_TOKEN, "t-env"),
            (ENV_SERVER, "ws://env"),
            (ENV_TARGET, "http://env"),
        ]);
        let s = resolve(&cli, &file, &e).unwrap();
        assert_eq!(s.token, "t-cli");
        assert_eq!(s.server, "ws://cli");
        assert_eq!(s.target, "http://cli");
    }

    #[test]
    fn resolve_env_beats_file_and_fills_gaps() {
        let cli = CliOverrides {
            token: Some("t-cli".into()),
            ..Default::default()
        };
        let file = FileConfig {
            token: Some("t-file".into()),
            server: Some("ws://file".into()),
            reconnect_secs: Some(9),
            ..Default::default()
        };
        let e = env(&[(ENV_TOKEN, "t-env"), (ENV_SERVER, "ws://env")]);
        let s = resolve(&cli, &file, &e).unwrap();
        assert_eq!(s.token, "t-cli");
        assert_eq!(s.server, "ws://env");
        assert_eq!(s.target, DEFAULT_TARGET);
        assert_eq!(s.reconnect, 9); // 数值旗标未传时回落配置文件
    }

    #[test]
    fn resolve_env_used_when_cli_absent() {
        let file = FileConfig {
            token: Some("t-file".into()),
            server: Some("ws://file".into()),
            ..Default::default()
        };
        let e = env(&[(ENV_TOKEN, "t-env")]);
        let s = resolve(&CliOverrides::default(), &file, &e).unwrap();
        assert_eq!(s.token, "t-env");
        assert_eq!(s.server, "ws://file"); // env 未提供 server → 配置文件
    }

    #[test]
    fn resolve_file_used_when_no_cli_or_env() {
        let file = FileConfig {
            token: Some("t-file".into()),
            target: Some("http://file".into()),
            reconnect_secs: Some(9),
            max_reconnect: Some(3),
            request_timeout_secs: Some(60),
            force: Some(true),
            ..Default::default()
        };
        let s = resolve(&CliOverrides::default(), &file, &env(&[])).unwrap();
        assert_eq!(s.token, "t-file");
        assert_eq!(s.target, "http://file");
        assert_eq!(s.reconnect, 9);
        assert_eq!(s.max_reconnect, 3);
        assert_eq!(s.request_timeout, 60);
        assert!(s.force);
    }

    #[test]
    fn resolve_builtin_defaults_when_nothing_set() {
        let e = env(&[(ENV_TOKEN, "t-env")]); // 仅补 token，其余走内置默认
        let s = resolve(&CliOverrides::default(), &FileConfig::default(), &e).unwrap();
        assert_eq!(s.token, "t-env");
        assert_eq!(s.server, DEFAULT_SERVER);
        assert_eq!(s.target, DEFAULT_TARGET);
        assert_eq!(s.reconnect, 5);
        assert_eq!(s.max_reconnect, 0);
        assert_eq!(s.request_timeout, 300);
        assert!(!s.force);
    }

    #[test]
    fn resolve_missing_token_is_error() {
        let err = resolve(&CliOverrides::default(), &FileConfig::default(), &env(&[])).unwrap_err();
        assert!(err.contains("token"), "{err}");
        // CLI 缺、env 有 → 不报错
        let e = env(&[(ENV_TOKEN, "t-env")]);
        assert!(resolve(&CliOverrides::default(), &FileConfig::default(), &e).is_ok());
    }

    #[test]
    fn toml_parses_all_fields() {
        let text = r#"
server = "wss://s.example/ws/tunnel"
token = "tok-1"
target = "http://127.0.0.1:3080"
reconnect_secs = 7
max_reconnect = 2
request_timeout_secs = 30
force = true
"#;
        let c: FileConfig = toml::from_str(text).unwrap();
        assert_eq!(c.server.as_deref(), Some("wss://s.example/ws/tunnel"));
        assert_eq!(c.token.as_deref(), Some("tok-1"));
        assert_eq!(c.target.as_deref(), Some("http://127.0.0.1:3080"));
        assert_eq!(c.reconnect_secs, Some(7));
        assert_eq!(c.max_reconnect, Some(2));
        assert_eq!(c.request_timeout_secs, Some(30));
        assert_eq!(c.force, Some(true));
    }

    #[test]
    fn toml_missing_fields_are_none() {
        let c: FileConfig = toml::from_str("token = \"x\"\n").unwrap();
        assert_eq!(c.token.as_deref(), Some("x"));
        assert_eq!(c.server, None);
        assert_eq!(c.target, None);
        assert_eq!(c.reconnect_secs, None);
        assert_eq!(c.max_reconnect, None);
        assert_eq!(c.request_timeout_secs, None);
        assert_eq!(c.force, None);
    }

    #[test]
    fn toml_malformed_is_rejected() {
        assert!(toml::from_str::<FileConfig>("token = ").is_err());
        assert!(toml::from_str::<FileConfig>("???").is_err());
        assert!(toml::from_str::<FileConfig>("reconnect_secs = \"abc\"").is_err());
    }

    #[test]
    fn load_config_file_missing_present_and_malformed() {
        let dir = tempfile::tempdir().unwrap();
        assert!(load_config_file(&dir.path().join("nope.toml"))
            .unwrap()
            .is_none());

        let ok = dir.path().join("client.toml");
        std::fs::write(&ok, "token = \"from-file\"\n").unwrap();
        let c = load_config_file(&ok).unwrap().unwrap();
        assert_eq!(c.token.as_deref(), Some("from-file"));

        let bad = dir.path().join("bad.toml");
        std::fs::write(&bad, "???").unwrap();
        let err = load_config_file(&bad).unwrap_err();
        assert!(err.contains("bad.toml"), "{err}");
    }

    #[test]
    fn load_first_config_takes_first_existing() {
        let dir = tempfile::tempdir().unwrap();
        let a = dir.path().join("a.toml");
        let b = dir.path().join("b.toml");
        std::fs::write(&b, "token = \"b\"\n").unwrap();
        let (cfg, used) = load_first_config(&[a, b.clone()]).unwrap();
        assert_eq!(cfg.token.as_deref(), Some("b"));
        assert_eq!(used.as_deref(), Some(b.as_path()));

        let (cfg2, used2) = load_first_config(&[dir.path().join("none.toml")]).unwrap();
        assert_eq!(cfg2.token, None);
        assert!(used2.is_none());
    }

    #[test]
    fn config_paths_order_and_home_fallback() {
        let paths = config_paths_with(Some("/home/u"));
        assert_eq!(paths.len(), 2);
        assert_eq!(paths[0], PathBuf::from("./tunely-client.toml"));
        assert_eq!(
            paths[1],
            PathBuf::from("/home/u/.config/tunely/client.toml")
        );
        assert_eq!(config_paths_with(None).len(), 1);
        assert_eq!(config_paths_with(Some("")).len(), 1);
    }
}
