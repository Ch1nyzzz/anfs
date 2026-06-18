// useless_conversion fires inside #[pymethods]-generated glue (pyo3 0.22 +
// recent clippy); the other two reflect the PyO3 keyword-argument API surface
// and tuple-shaped projection rows, which stay until rows become structs.
#![allow(clippy::useless_conversion)]
#![allow(clippy::too_many_arguments)]
#![allow(clippy::type_complexity)]

use pyo3::prelude::*;
use rusqlite::Connection;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

mod blob;
mod bundle;
mod checkout;
mod chunked;
mod common;
mod embedding;
mod engine;
mod errors;
mod events;
mod fragments;
mod gc_snapshot;
mod integrity;
mod lineage;
mod manifest;
mod naming;
mod observability;
mod policy_labels;
mod query;
mod refs;
mod replay;
mod runs;
mod schema;
mod span_parser;
mod types;
mod visibility;
mod workspace;
mod workspace_ops;
mod worktree;

use blob::*;
use bundle::*;
use checkout::*;
use chunked::*;
use common::*;
use embedding::*;
use errors::*;
use events::*;
use fragments::*;
use gc_snapshot::*;
use integrity::*;
use lineage::*;
use manifest::*;
use naming::*;
use observability::*;
use policy_labels::*;
use refs::*;
use replay::*;
use runs::*;
use schema::{
    apply_schema_migrations, init_db, reject_unsupported_future_schema, require_schema_current,
    schema_migration_plan, schema_migrations, schema_status,
};
use span_parser::*;
use types::*;
use visibility::*;
use workspace_ops::*;
use worktree::*;

const INLINE_THRESHOLD: usize = 64 * 1024;
const MANIFEST_MEDIA_TYPE: &str = "application/vnd.anfs.manifest+json";
const CHUNKED_MEDIA_TYPE: &str = "application/vnd.anfs.chunked+json";
const CURRENT_SCHEMA_VERSION: i64 = 1;
const CURRENT_SCHEMA_NAME: &str = "anfs_kernel_schema_v1";

struct Inner {
    conn: Mutex<Connection>,
    objects_dir: PathBuf,
}

#[pyclass]
struct AnfsEngine {
    inner: Arc<Inner>,
}

#[pyclass]
struct Workspace {
    inner: Arc<Inner>,
    workspace_name: String,
    agent_id: String,
    run_id: Option<String>,
}

#[pymodule]
fn anfs_core(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<AnfsEngine>()?;
    m.add_class::<Workspace>()?;
    m.add(
        "LineageMismatchError",
        py.get_type_bound::<LineageMismatchError>(),
    )?;
    m.add("RefConflictError", py.get_type_bound::<RefConflictError>())?;
    m.add(
        "InvalidStateTransitionError",
        py.get_type_bound::<InvalidStateTransitionError>(),
    )?;
    m.add("RefNotFoundError", py.get_type_bound::<RefNotFoundError>())?;
    m.add("RunNotFoundError", py.get_type_bound::<RunNotFoundError>())?;
    m.add(
        "NodeNotFoundError",
        py.get_type_bound::<NodeNotFoundError>(),
    )?;
    m.add(
        "EventNotFoundError",
        py.get_type_bound::<EventNotFoundError>(),
    )?;
    m.add(
        "PolicyDeniedError",
        py.get_type_bound::<PolicyDeniedError>(),
    )?;
    m.add(
        "StorageCorruptionError",
        py.get_type_bound::<StorageCorruptionError>(),
    )?;
    Ok(())
}
