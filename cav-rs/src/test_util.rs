//! Shared helpers for tests that mutate process-global state.

use std::fs;
use std::path::PathBuf;
use std::sync::Mutex;

/// Several tests share process-global state (the `CODING_CAVE_DATA_DIR`
/// env var and the SQLite registry it points at). Any test that mutates
/// that state must serialize on this lock.
pub(crate) static DATA_DIR_LOCK: Mutex<()> = Mutex::new(());

pub(crate) struct DataDirGuard {
    _lock: std::sync::MutexGuard<'static, ()>,
    prev: Option<String>,
    pub path: PathBuf,
}

impl DataDirGuard {
    pub fn new(label: &str) -> Self {
        let lock = DATA_DIR_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let prev = std::env::var("CODING_CAVE_DATA_DIR").ok();
        let tmp = std::env::temp_dir();
        let mut path = PathBuf::new();
        for i in 0..1000 {
            let p = tmp.join(format!("cav-test-{label}-{i}"));
            if fs::create_dir(&p).is_ok() {
                path = p;
                break;
            }
        }
        assert!(!path.as_os_str().is_empty(), "could not allocate tmp dir");
        std::env::set_var("CODING_CAVE_DATA_DIR", &path);
        Self { _lock: lock, prev, path }
    }
}

impl Drop for DataDirGuard {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
        match &self.prev {
            Some(v) => std::env::set_var("CODING_CAVE_DATA_DIR", v),
            None => std::env::remove_var("CODING_CAVE_DATA_DIR"),
        }
    }
}
