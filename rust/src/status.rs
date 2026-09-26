//! 运行状态文件：`connect` 在状态变化点把连接状态落盘，`status` 子命令读取展示。
//!
//! 状态文件路径解析顺序：环境变量 TUNELY_STATE_FILE > $HOME/.local/state/tunely/status.json。
//! 路径解析以闭包注入为纯函数，单测不触碰进程全局状态。

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use time::format_description::well_known::Rfc3339;
use time::OffsetDateTime;

use crate::config::{home_dir, real_env};

/// 覆盖状态文件路径的环境变量
pub const ENV_STATE_FILE: &str = "TUNELY_STATE_FILE";

/// 客户端运行状态（序列化为小写字符串）
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum RunState {
    Connected,
    Disconnected,
    Reconnecting,
}

impl RunState {
    pub fn as_str(self) -> &'static str {
        match self {
            RunState::Connected => "connected",
            RunState::Disconnected => "disconnected",
            RunState::Reconnecting => "reconnecting",
        }
    }
}

/// 状态文件内容（JSON）
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct StatusData {
    pub pid: u32,
    pub state: RunState,
    pub domain: Option<String>,
    pub reconnect_count: u32,
    pub last_error: Option<String>,
    /// RFC3339（UTC）
    pub updated_at: String,
}

/// 当前 UTC 时间的 RFC3339 表示
pub fn now_rfc3339() -> String {
    OffsetDateTime::now_utc()
        .format(&Rfc3339)
        .unwrap_or_default()
}

/// 写状态文件：父目录不存在则创建；先写同目录 `.tmp` 临时文件再 `rename` 原子替换，
/// 避免读侧（status 子命令）读到半截 JSON（F21）。
pub fn write_status(path: &Path, data: &StatusData) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent)?;
        }
    }
    let mut json = serde_json::to_string_pretty(data)
        .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
    json.push('\n');
    let tmp = tmp_path_for(path);
    fs::write(&tmp, json)?;
    if let Err(e) = fs::rename(&tmp, path) {
        // rename 失败时清理临时文件，避免残留垃圾
        let _ = fs::remove_file(&tmp);
        return Err(e);
    }
    Ok(())
}

/// 同目录临时文件路径：`<name>.tmp`（rename 在同一文件系统上才是原子操作）
fn tmp_path_for(path: &Path) -> PathBuf {
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| "status.json".to_string());
    path.with_file_name(format!("{name}.tmp"))
}

/// 读状态文件：文件不存在 → NotFound；内容损坏 → InvalidData。
pub fn read_status(path: &Path) -> io::Result<StatusData> {
    let text = fs::read_to_string(path)?;
    serde_json::from_str(&text).map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))
}

/// 状态文件路径解析：TUNELY_STATE_FILE 优先，其次 $HOME/.local/state/tunely/status.json；
/// 两者都拿不到 → None（状态文件功能不可用）。
pub fn state_path_with(
    env: &dyn Fn(&str) -> Option<String>,
    home: Option<&str>,
) -> Option<PathBuf> {
    if let Some(p) = env(ENV_STATE_FILE).filter(|s| !s.trim().is_empty()) {
        return Some(PathBuf::from(p));
    }
    home.map(|h| {
        PathBuf::from(h)
            .join(".local")
            .join("state")
            .join("tunely")
            .join("status.json")
    })
}

/// 用真实环境解析状态文件路径
pub fn default_state_path() -> Option<PathBuf> {
    state_path_with(&real_env, home_dir().as_deref())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample(state: RunState) -> StatusData {
        StatusData {
            pid: 4242,
            state,
            domain: Some("dsh.example.com".into()),
            reconnect_count: 3,
            last_error: Some("connection reset".into()),
            updated_at: now_rfc3339(),
        }
    }

    #[test]
    fn roundtrip_write_read_with_dir_creation() {
        let dir = tempfile::tempdir().unwrap();
        // 多级不存在的父目录 → write_status 应自动创建
        let path = dir.path().join("a").join("b").join("status.json");
        let data = sample(RunState::Reconnecting);
        write_status(&path, &data).unwrap();
        assert_eq!(read_status(&path).unwrap(), data);
    }

    #[test]
    fn roundtrip_null_fields() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("status.json");
        let data = StatusData {
            pid: 1,
            state: RunState::Connected,
            domain: None,
            reconnect_count: 0,
            last_error: None,
            updated_at: "2026-09-25T00:00:00Z".into(),
        };
        write_status(&path, &data).unwrap();
        assert_eq!(read_status(&path).unwrap(), data);
    }

    #[test]
    fn read_missing_is_not_found() {
        let dir = tempfile::tempdir().unwrap();
        let err = read_status(&dir.path().join("nope.json")).unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::NotFound);
    }

    #[test]
    fn write_status_is_atomic_and_leaves_no_tmp_leftover() {
        // F21：覆盖写走「临时文件 + rename」，不残留 .tmp，且读侧始终看到完整内容
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("status.json");
        write_status(&path, &sample(RunState::Connected)).unwrap();
        write_status(&path, &sample(RunState::Disconnected)).unwrap();

        assert_eq!(read_status(&path).unwrap().state, RunState::Disconnected);
        let entries: Vec<String> = std::fs::read_dir(dir.path())
            .unwrap()
            .filter_map(|e| e.ok())
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .collect();
        assert_eq!(entries, vec!["status.json".to_string()]);
    }

    #[test]
    fn read_corrupt_is_invalid_data() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("status.json");
        std::fs::write(&path, "{not json").unwrap();
        let err = read_status(&path).unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn json_shape_matches_spec() {
        let data = StatusData {
            pid: 7,
            state: RunState::Connected,
            domain: None,
            reconnect_count: 2,
            last_error: None,
            updated_at: "t".into(),
        };
        let j = serde_json::to_string(&data).unwrap();
        assert!(j.contains(r#""pid":7"#), "{j}");
        assert!(j.contains(r#""state":"connected""#), "{j}");
        assert!(j.contains(r#""domain":null"#), "{j}");
        assert!(j.contains(r#""reconnect_count":2"#), "{j}");
        assert!(j.contains(r#""last_error":null"#), "{j}");
        assert!(j.contains(r#""updated_at":"t""#), "{j}");
    }

    #[test]
    fn rfc3339_parses_back_as_utc() {
        let s = now_rfc3339();
        let t = OffsetDateTime::parse(&s, &Rfc3339).unwrap();
        assert_eq!(t.offset(), time::UtcOffset::UTC);
    }

    #[test]
    fn state_path_env_wins_over_home() {
        let env = |key: &str| (key == ENV_STATE_FILE).then(|| "/tmp/custom/state.json".to_string());
        assert_eq!(
            state_path_with(&env, Some("/home/u")),
            Some(PathBuf::from("/tmp/custom/state.json"))
        );
    }

    #[test]
    fn state_path_falls_back_to_home_or_none() {
        let env = |_key: &str| None;
        assert_eq!(
            state_path_with(&env, Some("/home/u")),
            Some(PathBuf::from("/home/u/.local/state/tunely/status.json"))
        );
        assert_eq!(state_path_with(&env, None), None);
        // 空白环境变量视为未设置
        let env_blank = |key: &str| (key == ENV_STATE_FILE).then(|| "   ".to_string());
        assert_eq!(
            state_path_with(&env_blank, Some("/home/u")),
            Some(PathBuf::from("/home/u/.local/state/tunely/status.json"))
        );
    }
}
