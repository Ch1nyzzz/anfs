use rusqlite::{Connection, ErrorCode};
use std::thread;
use std::time::Duration;
use std::time::{SystemTime, UNIX_EPOCH};
use uuid::Uuid;

use crate::{AnfsError, AnfsResult, Inner};

pub(crate) fn lock_conn(inner: &Inner) -> AnfsResult<std::sync::MutexGuard<'_, Connection>> {
    inner
        .conn
        .lock()
        .map_err(|_| AnfsError::StorageCorruption("sqlite connection lock poisoned".to_string()))
}

pub(crate) fn with_sqlite_busy_retry<T>(
    mut operation: impl FnMut() -> AnfsResult<T>,
) -> AnfsResult<T> {
    let mut delay = Duration::from_millis(25);
    for attempt in 0..40 {
        match operation() {
            Ok(value) => return Ok(value),
            Err(err) if is_sqlite_busy_or_locked(&err) && attempt < 39 => {
                thread::sleep(delay);
                delay = std::cmp::min(delay * 2, Duration::from_millis(500));
            }
            Err(err) => return Err(err),
        }
    }
    operation()
}

pub(crate) fn is_sqlite_busy_or_locked(err: &AnfsError) -> bool {
    let AnfsError::Sqlite(sqlite_err) = err else {
        return false;
    };
    match sqlite_err {
        rusqlite::Error::SqliteFailure(code, _) => {
            code.code == ErrorCode::DatabaseBusy || code.code == ErrorCode::DatabaseLocked
        }
        _ => false,
    }
}

pub(crate) fn media_type_for_content(content: &[u8]) -> &'static str {
    if std::str::from_utf8(content).is_ok() {
        "text/plain"
    } else {
        "application/octet-stream"
    }
}

pub(crate) fn now_millis() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time before unix epoch")
        .as_millis() as i64
}

pub(crate) fn new_node_id() -> String {
    format!("node:{}", Uuid::new_v4())
}

pub(crate) fn new_event_id() -> String {
    format!("event:{}", Uuid::new_v4())
}

pub(crate) fn new_pin_id() -> String {
    format!("pin:{}", Uuid::new_v4())
}

pub(crate) fn new_lock_id() -> String {
    format!("lock:{}", Uuid::new_v4())
}
