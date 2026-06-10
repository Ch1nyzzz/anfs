use pyo3::create_exception;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;

create_exception!(anfs_core, LineageMismatchError, PyException);
create_exception!(anfs_core, RefConflictError, PyException);
create_exception!(anfs_core, InvalidStateTransitionError, PyException);
create_exception!(anfs_core, RefNotFoundError, PyException);
create_exception!(anfs_core, RunNotFoundError, PyException);
create_exception!(anfs_core, NodeNotFoundError, PyException);
create_exception!(anfs_core, EventNotFoundError, PyException);
create_exception!(anfs_core, PolicyDeniedError, PyException);
create_exception!(anfs_core, StorageCorruptionError, PyException);

#[derive(Debug)]
pub(crate) enum AnfsError {
    LineageMismatch(String),
    RefConflict(String),
    InvalidStateTransition(String),
    RefNotFound(String),
    RunNotFound(String),
    NodeNotFound(String),
    EventNotFound(String),
    PolicyDenied(String),
    StorageCorruption(String),
    Sqlite(rusqlite::Error),
    Io(std::io::Error),
}

impl From<rusqlite::Error> for AnfsError {
    fn from(value: rusqlite::Error) -> Self {
        AnfsError::Sqlite(value)
    }
}

impl From<std::io::Error> for AnfsError {
    fn from(value: std::io::Error) -> Self {
        AnfsError::Io(value)
    }
}

impl From<AnfsError> for PyErr {
    fn from(value: AnfsError) -> Self {
        match value {
            AnfsError::LineageMismatch(msg) => LineageMismatchError::new_err(msg),
            AnfsError::RefConflict(msg) => RefConflictError::new_err(msg),
            AnfsError::InvalidStateTransition(msg) => InvalidStateTransitionError::new_err(msg),
            AnfsError::RefNotFound(msg) => RefNotFoundError::new_err(msg),
            AnfsError::RunNotFound(msg) => RunNotFoundError::new_err(msg),
            AnfsError::NodeNotFound(msg) => NodeNotFoundError::new_err(msg),
            AnfsError::EventNotFound(msg) => EventNotFoundError::new_err(msg),
            AnfsError::PolicyDenied(msg) => PolicyDeniedError::new_err(msg),
            AnfsError::StorageCorruption(msg) => StorageCorruptionError::new_err(msg),
            AnfsError::Sqlite(err) => PyException::new_err(format!("sqlite error: {err}")),
            AnfsError::Io(err) => PyException::new_err(format!("io error: {err}")),
        }
    }
}

pub(crate) type AnfsResult<T> = Result<T, AnfsError>;
