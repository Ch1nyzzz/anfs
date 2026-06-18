use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyTuple};
use rusqlite::{params, Connection, OptionalExtension, Transaction};
use serde_json::json;
use std::collections::HashSet;

use crate::query::{fts_literal_query, query_refs};
use crate::{
    active_purpose_capability_missing, ensure_node_exists, ensure_node_fragments_visible,
    ensure_purpose_allows_ref_node, ensure_purpose_allows_ref_node_range, fetch_ref, Inner,
    insert_blob, insert_edge, insert_event, lock_conn, materialize_blob, media_type_for_content,
    new_event_id, new_lock_id, new_node_id, node_fragments_hidden, node_manifest_metadata,
    now_millis, propagate_derived_policy_labels, propagate_fragment_policy_labels_for_blob,
    purpose_hides_ref_node, AnfsError, AnfsResult, ManifestChild, ManifestDoc, QueryRefRow,
    read_node_bytes, read_node_range, RefRecord, RefWriteMode, require_ref, require_state,
    sha256_hex, update_ref_state, upsert_ref, validate_state_transition, VisibilityPolicy,
    with_sqlite_busy_retry, workspace_logical_path, workspace_ref_name, write_chunked_in_tx,
    Workspace, MANIFEST_MEDIA_TYPE,
};
use std::collections::{BTreeMap, BTreeSet};

const DIRECTORY_MEDIA_TYPE: &str = "application/vnd.anfs.directory+json";

type PosixStatRow = (
    String,
    String,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<i64>,
    Option<String>,
    i64,
);
type PosixStatDetailRow = (
    String,
    String,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<i64>,
    Option<String>,
    i64,
    i64,
    i64,
    i64,
    i64,
    i64,
    i64,
    i64,
    i64,
);
type PosixLsRow = (
    String,
    String,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<i64>,
    i64,
);
type PosixFindRow = (String, String, String, String);
type PosixGrepRow = (String, i64, String, String, String);
type WorkspaceLockRow = (
    String,
    String,
    String,
    String,
    Option<String>,
    i64,
    Option<i64>,
);

struct WorkspacePathMetadata {
    mode: Option<i64>,
    uid: Option<i64>,
    gid: Option<i64>,
    atime_ms: Option<i64>,
    mtime_ms: Option<i64>,
    ctime_ms: Option<i64>,
}

#[derive(Clone)]
struct WorkspaceRefEntry {
    ref_name: String,
    logical_path: String,
    node_id: String,
    state: String,
    ref_version: i64,
}

#[pymethods]
impl Workspace {
    #[pyo3(signature = (logical_path, content, derived_from_nodes=None, tool_call_id=None))]
    fn write(
        &self,
        logical_path: &str,
        content: &[u8],
        derived_from_nodes: Option<Vec<String>>,
        tool_call_id: Option<String>,
    ) -> PyResult<String> {
        self.write_impl(
            logical_path,
            content,
            derived_from_nodes.unwrap_or_default(),
            tool_call_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (logical_path, offset, content, tool_call_id=None))]
    fn write_range(
        &self,
        logical_path: &str,
        offset: i64,
        content: &[u8],
        tool_call_id: Option<String>,
    ) -> PyResult<String> {
        self.write_path_range_impl(logical_path, offset, content, tool_call_id)
            .map_err(PyErr::from)
    }

    /// Store a large file as content-addressed chunk blobs plus a manifest node.
    /// Identical chunks dedupe across files and edits. Returns the node id.
    #[pyo3(signature = (logical_path, content, chunk_size=65536, tool_call_id=None))]
    fn write_chunked(
        &self,
        logical_path: &str,
        content: &[u8],
        chunk_size: i64,
        tool_call_id: Option<String>,
    ) -> PyResult<String> {
        self.write_chunked_impl(logical_path, content, chunk_size, tool_call_id)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (logical_path, content, tool_call_id=None))]
    fn append(
        &self,
        logical_path: &str,
        content: &[u8],
        tool_call_id: Option<String>,
    ) -> PyResult<String> {
        self.append_path_impl(logical_path, content, tool_call_id)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (logical_path, length, tool_call_id=None))]
    fn truncate(
        &self,
        logical_path: &str,
        length: i64,
        tool_call_id: Option<String>,
    ) -> PyResult<String> {
        self.truncate_path_impl(logical_path, length, tool_call_id)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (logical_path, artifact_ref_name, kind="artifact", tool_call_id=None))]
    fn publish(
        &self,
        logical_path: &str,
        artifact_ref_name: &str,
        kind: &str,
        tool_call_id: Option<String>,
    ) -> PyResult<String> {
        self.publish_impl(logical_path, artifact_ref_name, kind, tool_call_id)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (logical_paths, artifact_ref_name, kind="artifact", tool_call_id=None))]
    fn publish_manifest(
        &self,
        logical_paths: Vec<String>,
        artifact_ref_name: &str,
        kind: &str,
        tool_call_id: Option<String>,
    ) -> PyResult<String> {
        self.publish_manifest_impl(logical_paths, artifact_ref_name, kind, tool_call_id)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (ref_name, purpose=None, tool_call_id=None))]
    fn consume(
        &self,
        ref_name: &str,
        purpose: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<String> {
        self.consume_impl(ref_name, purpose, tool_call_id)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (ref_name, purpose=None, tool_call_id=None))]
    fn read_ref<'py>(
        &self,
        py: Python<'py>,
        ref_name: &str,
        purpose: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<Bound<'py, PyBytes>> {
        let bytes = self
            .read_ref_impl(ref_name, purpose, tool_call_id)
            .map_err(PyErr::from)?;
        Ok(PyBytes::new_bound(py, &bytes))
    }

    #[pyo3(signature = (logical_path, purpose=None, tool_call_id=None))]
    fn read<'py>(
        &self,
        py: Python<'py>,
        logical_path: &str,
        purpose: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<Bound<'py, PyBytes>> {
        let bytes = self
            .read_path_impl(logical_path, purpose, tool_call_id)
            .map_err(PyErr::from)?;
        Ok(PyBytes::new_bound(py, &bytes))
    }

    #[pyo3(signature = (logical_path, offset, length, purpose=None, tool_call_id=None))]
    fn read_range<'py>(
        &self,
        py: Python<'py>,
        logical_path: &str,
        offset: i64,
        length: i64,
        purpose: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<Bound<'py, PyBytes>> {
        let bytes = self
            .read_path_range_impl(logical_path, offset, length, purpose, tool_call_id)
            .map_err(PyErr::from)?;
        Ok(PyBytes::new_bound(py, &bytes))
    }

    #[pyo3(signature = (logical_path, purpose=None, tool_call_id=None))]
    fn cat<'py>(
        &self,
        py: Python<'py>,
        logical_path: &str,
        purpose: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<Bound<'py, PyBytes>> {
        let bytes = self
            .read_path_impl(logical_path, purpose, tool_call_id)
            .map_err(PyErr::from)?;
        Ok(PyBytes::new_bound(py, &bytes))
    }

    #[pyo3(signature = (logical_path=""))]
    fn stat(&self, logical_path: &str) -> PyResult<PosixStatRow> {
        self.stat_impl(logical_path).map_err(PyErr::from)
    }

    #[pyo3(signature = (logical_path=""))]
    fn stat_posix<'py>(
        &self,
        py: Python<'py>,
        logical_path: &str,
    ) -> PyResult<Bound<'py, PyTuple>> {
        let row = self.stat_posix_impl(logical_path).map_err(PyErr::from)?;
        Ok(PyTuple::new_bound(
            py,
            vec![
                row.0.into_py(py),
                row.1.into_py(py),
                row.2.into_py(py),
                row.3.into_py(py),
                row.4.into_py(py),
                row.5.into_py(py),
                row.6.into_py(py),
                row.7.into_py(py),
                row.8.into_py(py),
                row.9.into_py(py),
                row.10.into_py(py),
                row.11.into_py(py),
                row.12.into_py(py),
                row.13.into_py(py),
                row.14.into_py(py),
                row.15.into_py(py),
            ],
        ))
    }

    #[pyo3(signature = (logical_path))]
    fn exists(&self, logical_path: &str) -> PyResult<bool> {
        self.path_kind_matches(logical_path, None)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (logical_path))]
    fn is_file(&self, logical_path: &str) -> PyResult<bool> {
        self.path_kind_matches(logical_path, Some("file"))
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (logical_path))]
    fn is_dir(&self, logical_path: &str) -> PyResult<bool> {
        self.path_kind_matches(logical_path, Some("directory"))
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (logical_path, mode, uid=None, gid=None, groups=None))]
    fn access(
        &self,
        logical_path: &str,
        mode: i64,
        uid: Option<i64>,
        gid: Option<i64>,
        groups: Option<Vec<i64>>,
    ) -> PyResult<bool> {
        self.access_impl(logical_path, mode, uid, gid, groups.unwrap_or_default())
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (logical_path, mode, tool_call_id=None))]
    fn chmod(&self, logical_path: &str, mode: i64, tool_call_id: Option<String>) -> PyResult<()> {
        self.chmod_impl(logical_path, mode, tool_call_id)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (logical_path, uid, gid, tool_call_id=None))]
    fn chown(
        &self,
        logical_path: &str,
        uid: i64,
        gid: i64,
        tool_call_id: Option<String>,
    ) -> PyResult<()> {
        self.chown_impl(logical_path, uid, gid, tool_call_id)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (logical_path, atime_ms=None, mtime_ms=None, tool_call_id=None))]
    fn utime(
        &self,
        logical_path: &str,
        atime_ms: Option<i64>,
        mtime_ms: Option<i64>,
        tool_call_id: Option<String>,
    ) -> PyResult<()> {
        self.utime_impl(logical_path, atime_ms, mtime_ms, tool_call_id)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (logical_path=""))]
    fn ls(&self, logical_path: &str) -> PyResult<Vec<PosixLsRow>> {
        self.ls_impl(logical_path).map_err(PyErr::from)
    }

    #[pyo3(signature = (logical_path, tool_call_id=None))]
    fn mkdir(&self, logical_path: &str, tool_call_id: Option<String>) -> PyResult<String> {
        self.mkdir_impl(logical_path, tool_call_id)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (logical_path, tool_call_id=None))]
    fn touch(&self, logical_path: &str, tool_call_id: Option<String>) -> PyResult<String> {
        self.touch_impl(logical_path, tool_call_id)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (logical_path, ttl_ms=None, tool_call_id=None))]
    fn acquire_lock(
        &self,
        logical_path: &str,
        ttl_ms: Option<i64>,
        tool_call_id: Option<String>,
    ) -> PyResult<String> {
        self.acquire_lock_impl(logical_path, ttl_ms, tool_call_id)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (logical_path, lock_id, tool_call_id=None))]
    fn release_lock(
        &self,
        logical_path: &str,
        lock_id: &str,
        tool_call_id: Option<String>,
    ) -> PyResult<bool> {
        self.release_lock_impl(logical_path, lock_id, tool_call_id)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (logical_path=""))]
    fn lock_info(&self, logical_path: &str) -> PyResult<Vec<WorkspaceLockRow>> {
        self.lock_info_impl(logical_path).map_err(PyErr::from)
    }

    #[pyo3(signature = (logical_path, tool_call_id=None, uid=None, gid=None, groups=None))]
    fn delete(
        &self,
        logical_path: &str,
        tool_call_id: Option<String>,
        uid: Option<i64>,
        gid: Option<i64>,
        groups: Option<Vec<i64>>,
    ) -> PyResult<()> {
        self.delete_impl(
            logical_path,
            tool_call_id,
            uid,
            gid,
            groups.unwrap_or_default(),
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (logical_path, tool_call_id=None, uid=None, gid=None, groups=None))]
    fn rm(
        &self,
        logical_path: &str,
        tool_call_id: Option<String>,
        uid: Option<i64>,
        gid: Option<i64>,
        groups: Option<Vec<i64>>,
    ) -> PyResult<()> {
        self.delete_impl(
            logical_path,
            tool_call_id,
            uid,
            gid,
            groups.unwrap_or_default(),
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (src_path, dst_path, tool_call_id=None))]
    fn cp(&self, src_path: &str, dst_path: &str, tool_call_id: Option<String>) -> PyResult<String> {
        self.cp_impl(src_path, dst_path, tool_call_id)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (src_path, dst_path, tool_call_id=None))]
    fn link(
        &self,
        src_path: &str,
        dst_path: &str,
        tool_call_id: Option<String>,
    ) -> PyResult<String> {
        self.link_impl(src_path, dst_path, tool_call_id)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (src_path, dst_path, tool_call_id=None, uid=None, gid=None, groups=None))]
    fn mv(
        &self,
        src_path: &str,
        dst_path: &str,
        tool_call_id: Option<String>,
        uid: Option<i64>,
        gid: Option<i64>,
        groups: Option<Vec<i64>>,
    ) -> PyResult<String> {
        self.mv_impl(
            src_path,
            dst_path,
            tool_call_id,
            uid,
            gid,
            groups.unwrap_or_default(),
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (logical_path="", pattern=None, tool_call_id=None, limit=None))]
    fn find(
        &self,
        logical_path: &str,
        pattern: Option<String>,
        tool_call_id: Option<String>,
        limit: Option<usize>,
    ) -> PyResult<Vec<PosixFindRow>> {
        self.find_impl(logical_path, pattern.as_deref(), tool_call_id, limit)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (pattern, logical_path="", tool_call_id=None, limit=None))]
    fn grep(
        &self,
        pattern: &str,
        logical_path: &str,
        tool_call_id: Option<String>,
        limit: Option<usize>,
    ) -> PyResult<Vec<PosixGrepRow>> {
        self.grep_impl(pattern, logical_path, tool_call_id, limit)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (query, scope="published", tool_call_id=None, policy_label_excludes=None, purpose=None))]
    fn search(
        &self,
        query: &str,
        scope: &str,
        tool_call_id: Option<String>,
        policy_label_excludes: Option<Vec<String>>,
        purpose: Option<String>,
    ) -> PyResult<Vec<(String, String)>> {
        self.search_impl(
            query,
            scope,
            tool_call_id,
            policy_label_excludes.unwrap_or_default(),
            purpose,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (prefix=None, text=None, state=None, agent_id=None, run_id=None, event_kind=None, media_type=None, created_after_ms=None, created_before_ms=None, policy=None, decision=None, reason_code=None, limit=100, tool_call_id=None, policy_label_excludes=None, purpose=None))]
    fn query(
        &self,
        prefix: Option<String>,
        text: Option<String>,
        state: Option<String>,
        agent_id: Option<String>,
        run_id: Option<String>,
        event_kind: Option<String>,
        media_type: Option<String>,
        created_after_ms: Option<i64>,
        created_before_ms: Option<i64>,
        policy: Option<String>,
        decision: Option<String>,
        reason_code: Option<String>,
        limit: i64,
        tool_call_id: Option<String>,
        policy_label_excludes: Option<Vec<String>>,
        purpose: Option<String>,
    ) -> PyResult<Vec<QueryRefRow>> {
        self.query_impl(
            prefix,
            text,
            state,
            agent_id,
            run_id,
            event_kind,
            media_type,
            created_after_ms,
            created_before_ms,
            policy,
            decision,
            reason_code,
            limit,
            tool_call_id,
            policy_label_excludes.unwrap_or_default(),
            purpose,
        )
        .map_err(PyErr::from)
    }

}

impl Workspace {
    pub(crate) fn write_impl(
        &self,
        logical_path: &str,
        content: &[u8],
        derived_from_nodes: Vec<String>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<String> {
        with_sqlite_busy_retry(|| {
            let mut conn = lock_conn(&self.inner)?;
            let tx = conn.transaction()?;
            let node_id = self.write_in_tx(
                &tx,
                logical_path,
                content,
                derived_from_nodes.clone(),
                tool_call_id.as_deref(),
            )?;
            tx.commit()?;
            Ok(node_id)
        })
    }

    pub(crate) fn write_in_tx(
        &self,
        tx: &Transaction<'_>,
        logical_path: &str,
        content: &[u8],
        derived_from_nodes: Vec<String>,
        tool_call_id: Option<&str>,
    ) -> AnfsResult<String> {
        self.write_content_in_tx(
            tx,
            logical_path,
            content,
            derived_from_nodes,
            "write",
            logical_path.to_string(),
            tool_call_id,
        )
    }

    pub(crate) fn write_chunked_impl(
        &self,
        logical_path: &str,
        content: &[u8],
        chunk_size: i64,
        tool_call_id: Option<String>,
    ) -> AnfsResult<String> {
        with_sqlite_busy_retry(|| {
            let mut conn = lock_conn(&self.inner)?;
            let tx = conn.transaction()?;
            let node_id = write_chunked_in_tx(
                &tx,
                &self.inner.objects_dir,
                &self.workspace_name,
                &self.agent_id,
                self.run_id.as_deref(),
                tool_call_id.as_deref(),
                logical_path,
                content,
                chunk_size,
            )?;
            tx.commit()?;
            Ok(node_id)
        })
    }

    fn write_content_in_tx(
        &self,
        tx: &Transaction<'_>,
        logical_path: &str,
        content: &[u8],
        derived_from_nodes: Vec<String>,
        event_kind: &str,
        event_payload: String,
        tool_call_id: Option<&str>,
    ) -> AnfsResult<String> {
        let ref_name = workspace_ref_name(&self.workspace_name, logical_path);
        require_path_unlocked_for_mutation(tx, self, logical_path, false, now_millis())?;
        let hash = sha256_hex(content);
        let blob_storage = materialize_blob(&self.inner.objects_dir, &hash, content)?;
        let old_ref = fetch_ref(tx, &ref_name)?;
        let created_new_path = old_ref
            .as_ref()
            .is_none_or(|old| old.state == "deleted" || old.state == "archived");

        for node_id in &derived_from_nodes {
            ensure_node_exists(tx, node_id)?;
        }

        insert_blob(tx, &hash, content.len() as i64, &blob_storage)?;

        let node_id = new_node_id();
        tx.execute(
            "INSERT INTO nodes (node_id, blob_hash, kind, media_type, metadata_json, created_at)
             VALUES (?1, ?2, 'artifact', ?3, ?4, ?5)",
            params![
                node_id,
                hash,
                media_type_for_content(content),
                "{}",
                now_millis()
            ],
        )?;
        propagate_fragment_policy_labels_for_blob(
            tx,
            &node_id,
            &hash,
            &self.agent_id,
            self.run_id.as_deref(),
            tool_call_id,
        )?;
        propagate_derived_policy_labels(
            tx,
            &node_id,
            &hash,
            content.len() as i64,
            &derived_from_nodes,
            &self.agent_id,
            self.run_id.as_deref(),
            tool_call_id,
        )?;

        if let Ok(body) = std::str::from_utf8(content) {
            tx.execute(
                "INSERT INTO node_fts (node_id, body) VALUES (?1, ?2)",
                params![node_id, body],
            )?;
        }

        let event_id = new_event_id();
        insert_event(
            tx,
            &event_id,
            event_kind,
            Some(&self.agent_id),
            self.run_id.as_deref(),
            tool_call_id,
            Some(&self.workspace_name),
            Some(&event_payload),
        )?;
        for input_node in &derived_from_nodes {
            insert_edge(tx, &event_id, "input", input_node, "source", None)?;
        }
        insert_edge(
            tx,
            &event_id,
            "output",
            &node_id,
            "result",
            Some(logical_path),
        )?;

        upsert_ref(
            tx,
            &ref_name,
            &node_id,
            "workspace",
            "draft",
            &event_id,
            RefWriteMode::WorkspaceDraft,
        )?;
        if created_new_path {
            inherit_parent_setgid_metadata(
                tx,
                &self.workspace_name,
                logical_path,
                "file",
                &event_id,
            )?;
        }
        Ok(node_id)
    }

    fn publish_impl(
        &self,
        logical_path: &str,
        artifact_ref_name: &str,
        kind: &str,
        tool_call_id: Option<String>,
    ) -> AnfsResult<String> {
        let ws_ref = workspace_ref_name(&self.workspace_name, logical_path);
        let mut conn = lock_conn(&self.inner)?;
        let tx = conn.transaction()?;
        let draft =
            require_ref(&tx, &ws_ref)?;
        require_state(&draft.state, "draft", "publish")?;

        let event_id = new_event_id();
        insert_event(
            &tx,
            &event_id,
            "publish",
            Some(&self.agent_id),
            self.run_id.as_deref(),
            tool_call_id.as_deref(),
            Some(&self.workspace_name),
            Some(artifact_ref_name),
        )?;
        insert_edge(
            &tx,
            &event_id,
            "input",
            &draft.node_id,
            "workspace_node",
            Some(logical_path),
        )?;
        insert_edge(
            &tx,
            &event_id,
            "output",
            &draft.node_id,
            "published_ref",
            Some(artifact_ref_name),
        )?;

        upsert_ref(
            &tx,
            artifact_ref_name,
            &draft.node_id,
            kind,
            "published",
            &event_id,
            RefWriteMode::PublishImmutable,
        )?;
        tx.commit()?;
        Ok(draft.node_id)
    }

    fn publish_manifest_impl(
        &self,
        logical_paths: Vec<String>,
        artifact_ref_name: &str,
        kind: &str,
        tool_call_id: Option<String>,
    ) -> AnfsResult<String> {
        if logical_paths.is_empty() {
            return Err(AnfsError::PolicyDenied(
                "manifest publish requires at least one logical path".to_string(),
            ));
        }

        let mut paths = logical_paths;
        paths.sort();
        paths.dedup();

        let mut conn = lock_conn(&self.inner)?;
        let tx = conn.transaction()?;

        let mut children = Vec::with_capacity(paths.len());
        for logical_path in &paths {
            let ws_ref = workspace_ref_name(&self.workspace_name, logical_path);
            let draft =
                require_ref(&tx, &ws_ref)?;
            require_state(&draft.state, "draft", "publish manifest")?;
            let (digest, media_type, size) = node_manifest_metadata(&tx, &draft.node_id)?;
            children.push(ManifestChild {
                path: logical_path.trim_start_matches('/').to_string(),
                node_id: draft.node_id,
                role: Some("child".to_string()),
                media_type,
                digest: Some(digest),
                size: Some(size),
            });
        }

        let manifest = ManifestDoc {
            schema: "anfs.manifest.v1".to_string(),
            kind: kind.to_string(),
            children,
        };
        let manifest_bytes = serde_json::to_vec(&manifest).map_err(|err| {
            AnfsError::StorageCorruption(format!("failed to serialize manifest: {err}"))
        })?;
        let hash = sha256_hex(&manifest_bytes);
        let blob_storage = materialize_blob(&self.inner.objects_dir, &hash, &manifest_bytes)?;
        insert_blob(&tx, &hash, manifest_bytes.len() as i64, &blob_storage)?;

        let manifest_node_id = new_node_id();
        let metadata = json!({
            "manifest": true,
            "schema": "anfs.manifest.v1",
            "child_count": manifest.children.len(),
        })
        .to_string();
        tx.execute(
            "INSERT INTO nodes (node_id, blob_hash, kind, media_type, metadata_json, created_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                manifest_node_id,
                hash,
                kind,
                MANIFEST_MEDIA_TYPE,
                metadata,
                now_millis()
            ],
        )?;
        tx.execute(
            "INSERT INTO node_fts (node_id, body) VALUES (?1, ?2)",
            params![
                manifest_node_id,
                String::from_utf8_lossy(&manifest_bytes).as_ref()
            ],
        )?;

        let event_id = new_event_id();
        insert_event(
            &tx,
            &event_id,
            "publish_manifest",
            Some(&self.agent_id),
            self.run_id.as_deref(),
            tool_call_id.as_deref(),
            Some(&self.workspace_name),
            Some(artifact_ref_name),
        )?;
        for (idx, child) in manifest.children.iter().enumerate() {
            let role = format!("manifest_child:{idx}");
            insert_edge(
                &tx,
                &event_id,
                "input",
                &child.node_id,
                &role,
                Some(&child.path),
            )?;
        }
        insert_edge(
            &tx,
            &event_id,
            "output",
            &manifest_node_id,
            "manifest",
            Some(artifact_ref_name),
        )?;

        upsert_ref(
            &tx,
            artifact_ref_name,
            &manifest_node_id,
            kind,
            "published",
            &event_id,
            RefWriteMode::PublishImmutable,
        )?;
        tx.commit()?;
        Ok(manifest_node_id)
    }

    fn consume_impl(
        &self,
        ref_name: &str,
        purpose: Option<String>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<String> {
        let mut conn = lock_conn(&self.inner)?;
        let tx = conn.transaction()?;
        let rec = require_ref(&tx, ref_name)?;
        if rec.state != "published" && rec.state != "approved" {
            return Err(AnfsError::PolicyDenied(format!(
                "cannot consume ref {ref_name} in state {}",
                rec.state
            )));
        }
        if let Some(purpose_value) = purpose.as_deref() {
            self.require_purpose_capability(&tx, purpose_value, "consume")?;
            ensure_purpose_allows_ref_node(&tx, purpose_value, ref_name, &rec.node_id, || {
                format!(
                    "consume for purpose {purpose_value} blocked by policy label on ref {ref_name} or node {}",
                    rec.node_id
                )
            })?;
        }

        let event_id = new_event_id();
        let payload = json!({
            "ref_name": ref_name,
            "node_id": rec.node_id.clone(),
            "state": rec.state.clone(),
            "ref_version": rec.ref_version,
            "purpose": purpose,
        })
        .to_string();
        insert_event(
            &tx,
            &event_id,
            "consume",
            Some(&self.agent_id),
            self.run_id.as_deref(),
            tool_call_id.as_deref(),
            Some(&self.workspace_name),
            Some(&payload),
        )?;
        insert_edge(
            &tx,
            &event_id,
            "input",
            &rec.node_id,
            "consumed",
            Some(ref_name),
        )?;
        tx.commit()?;
        Ok(rec.node_id)
    }

    fn read_ref_impl(
        &self,
        ref_name: &str,
        purpose: Option<String>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<Vec<u8>> {
        with_sqlite_busy_retry(|| {
            let mut conn = lock_conn(&self.inner)?;
            let tx = conn.transaction()?;
            let rec = require_ref(&tx, ref_name)?;
            let readable = rec.state == "published"
                || rec.state == "approved"
                || self.can_read_draft_ref(ref_name, &rec);
            if !readable {
                return Err(AnfsError::PolicyDenied(format!(
                    "cannot read ref {ref_name} in state {} from workspace {}",
                    rec.state, self.workspace_name
                )));
            }
            ensure_node_fragments_visible(&tx, &rec.node_id, || {
                format!(
                    "read_ref blocked by fragment policy label on node {}",
                    rec.node_id
                )
            })?;
            if let Some(purpose_value) = purpose.as_deref() {
                self.require_purpose_capability(&tx, purpose_value, "read_ref")?;
                ensure_purpose_allows_ref_node(&tx, purpose_value, ref_name, &rec.node_id, || {
                    format!(
                        "read_ref for purpose {purpose_value} blocked by policy label on ref {ref_name} or node {}",
                        rec.node_id
                    )
                })?;
            }

            let bytes = read_node_bytes(&tx, &self.inner.objects_dir, &rec.node_id)?;
            let event_id = new_event_id();
            let payload = json!({
                "ref_name": ref_name,
                "node_id": rec.node_id.clone(),
                "state": rec.state.clone(),
                "ref_version": rec.ref_version,
                "purpose": purpose,
            })
            .to_string();
            insert_event(
                &tx,
                &event_id,
                "read_ref",
                Some(&self.agent_id),
                self.run_id.as_deref(),
                tool_call_id.as_deref(),
                Some(&self.workspace_name),
                Some(&payload),
            )?;
            insert_edge(
                &tx,
                &event_id,
                "input",
                &rec.node_id,
                "read_ref",
                Some(ref_name),
            )?;
            tx.commit()?;
            Ok(bytes)
        })
    }

    fn can_read_draft_ref(&self, ref_name: &str, rec: &RefRecord) -> bool {
        rec.state == "draft" && workspace_logical_path(&self.workspace_name, ref_name).is_some()
    }

    fn read_path_impl(
        &self,
        logical_path: &str,
        purpose: Option<String>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<Vec<u8>> {
        let ref_name = self.resolve_path_ref(logical_path);
        let rec = {
            let conn = lock_conn(&self.inner)?;
            require_ref(&conn, &ref_name)?
        };
        if is_directory_ref_name(&ref_name) || is_directory_node(&self.inner, &rec.node_id)? {
            return Err(AnfsError::PolicyDenied(format!(
                "cannot read directory path {logical_path}"
            )));
        }
        self.read_ref_impl(&ref_name, purpose, tool_call_id)
    }

    fn read_path_range_impl(
        &self,
        logical_path: &str,
        offset: i64,
        length: i64,
        purpose: Option<String>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<Vec<u8>> {
        with_sqlite_busy_retry(|| {
            let ref_name = self.resolve_path_ref(logical_path);
            let mut conn = lock_conn(&self.inner)?;
            let tx = conn.transaction()?;
            let rec = require_ref(&tx, &ref_name)?;
            if is_directory_ref_name(&ref_name) || is_directory_node_tx(&tx, &rec.node_id)? {
                return Err(AnfsError::PolicyDenied(format!(
                    "cannot read directory path {logical_path}"
                )));
            }
            require_path_readable(self, &ref_name, &rec)?;
            if let Some(purpose_value) = purpose.as_deref() {
                self.require_purpose_capability(&tx, purpose_value, "read_range")?;
                ensure_purpose_allows_ref_node_range(
                    &tx,
                    purpose_value,
                    &ref_name,
                    &rec.node_id,
                    offset,
                    length,
                    || {
                        format!(
                            "read_range for purpose {purpose_value} blocked by policy label on ref {ref_name}, node {}, or requested fragment range",
                            rec.node_id
                        )
                    },
                )?;
            }

            let bytes =
                read_node_range(&tx, &self.inner.objects_dir, &rec.node_id, offset, length)?;
            let event_id = new_event_id();
            let payload = json!({
                "path": normalize_logical_path(logical_path),
                "ref_name": ref_name.clone(),
                "node_id": rec.node_id.clone(),
                "state": rec.state.clone(),
                "ref_version": rec.ref_version,
                "offset": offset,
                "length": length,
                "actual_length": bytes.len(),
                "purpose": purpose,
            })
            .to_string();
            insert_event(
                &tx,
                &event_id,
                "read_range",
                Some(&self.agent_id),
                self.run_id.as_deref(),
                tool_call_id.as_deref(),
                Some(&self.workspace_name),
                Some(&payload),
            )?;
            insert_edge(
                &tx,
                &event_id,
                "input",
                &rec.node_id,
                "read_range",
                Some(&ref_name),
            )?;
            tx.commit()?;
            Ok(bytes)
        })
    }

    fn write_path_range_impl(
        &self,
        logical_path: &str,
        offset: i64,
        content: &[u8],
        tool_call_id: Option<String>,
    ) -> AnfsResult<String> {
        if offset < 0 {
            return Err(AnfsError::PolicyDenied(
                "range offset must be non-negative".to_string(),
            ));
        }
        with_sqlite_busy_retry(|| {
            let path = normalize_logical_path(logical_path);
            if path.is_empty() || is_explicit_ref(logical_path) {
                return Err(AnfsError::PolicyDenied(
                    "write_range requires a workspace logical file path".to_string(),
                ));
            }
            let ref_name = workspace_ref_name(&self.workspace_name, &path);
            let mut conn = lock_conn(&self.inner)?;
            let tx = conn.transaction()?;
            let rec = if let Some(rec) = fetch_ref(&tx, &ref_name)? {
                rec
            } else {
                let dir_ref_name =
                    workspace_ref_name(&self.workspace_name, &normalize_dir_path(&path));
                if fetch_ref(&tx, &dir_ref_name)?.is_some() {
                    return Err(AnfsError::PolicyDenied(format!(
                        "cannot write_range directory path {logical_path}"
                    )));
                }
                return Err(AnfsError::RefNotFound(ref_name.clone()));
            };
            if is_directory_ref_name(&ref_name) || is_directory_node_tx(&tx, &rec.node_id)? {
                return Err(AnfsError::PolicyDenied(format!(
                    "cannot write_range directory path {logical_path}"
                )));
            }
            require_path_readable(self, &ref_name, &rec)?;
            ensure_node_fragments_visible(&tx, &rec.node_id, || {
                format!(
                    "write_range blocked by fragment policy label on source node {}",
                    rec.node_id
                )
            })?;

            if content.is_empty() {
                return Ok(rec.node_id);
            }

            let old_bytes = read_node_bytes(&tx, &self.inner.objects_dir, &rec.node_id)?;
            let offset_usize = usize::try_from(offset)
                .map_err(|_| AnfsError::PolicyDenied("range offset is too large".to_string()))?;
            let write_end = offset_usize
                .checked_add(content.len())
                .ok_or_else(|| AnfsError::PolicyDenied("range write is too large".to_string()))?;
            let zero_fill_length = offset_usize.saturating_sub(old_bytes.len());
            let replace_end = if offset_usize < old_bytes.len() {
                std::cmp::min(old_bytes.len(), write_end)
            } else {
                old_bytes.len()
            };
            let new_length = std::cmp::max(old_bytes.len(), write_end);
            let mut new_bytes = Vec::with_capacity(new_length);
            if offset_usize <= old_bytes.len() {
                new_bytes.extend_from_slice(&old_bytes[..offset_usize]);
            } else {
                new_bytes.extend_from_slice(&old_bytes);
                new_bytes.resize(offset_usize, 0);
            }
            new_bytes.extend_from_slice(content);
            new_bytes.extend_from_slice(&old_bytes[replace_end..]);
            let payload = json!({
                "path": path.clone(),
                "ref_name": ref_name,
                "old_node_id": rec.node_id.clone(),
                "offset": offset,
                "write_length": content.len(),
                "zero_fill_length": zero_fill_length,
                "old_length": old_bytes.len(),
                "new_length": new_bytes.len(),
            })
            .to_string();
            let node_id = self.write_content_in_tx(
                &tx,
                &path,
                &new_bytes,
                vec![rec.node_id],
                "write_range",
                payload,
                tool_call_id.as_deref(),
            )?;
            tx.commit()?;
            Ok(node_id)
        })
    }

    fn append_path_impl(
        &self,
        logical_path: &str,
        content: &[u8],
        tool_call_id: Option<String>,
    ) -> AnfsResult<String> {
        with_sqlite_busy_retry(|| {
            let path = normalize_logical_path(logical_path);
            if path.is_empty() || is_explicit_ref(logical_path) {
                return Err(AnfsError::PolicyDenied(
                    "append requires a workspace logical file path".to_string(),
                ));
            }
            let ref_name = workspace_ref_name(&self.workspace_name, &path);
            let mut conn = lock_conn(&self.inner)?;
            let tx = conn.transaction()?;
            let existing = if let Some(rec) = fetch_ref(&tx, &ref_name)? {
                Some(rec)
            } else {
                let dir_ref_name =
                    workspace_ref_name(&self.workspace_name, &normalize_dir_path(&path));
                if fetch_ref(&tx, &dir_ref_name)?.is_some() {
                    return Err(AnfsError::PolicyDenied(format!(
                        "cannot append directory path {logical_path}"
                    )));
                }
                None
            };

            let (old_node_id, old_bytes) = if let Some(rec) = existing {
                if is_directory_ref_name(&ref_name) || is_directory_node_tx(&tx, &rec.node_id)? {
                    return Err(AnfsError::PolicyDenied(format!(
                        "cannot append directory path {logical_path}"
                    )));
                }
                require_path_readable(self, &ref_name, &rec)?;
                ensure_node_fragments_visible(&tx, &rec.node_id, || {
                    format!(
                        "append blocked by fragment policy label on source node {}",
                        rec.node_id
                    )
                })?;
                let bytes = read_node_bytes(&tx, &self.inner.objects_dir, &rec.node_id)?;
                (Some(rec.node_id), bytes)
            } else {
                (None, Vec::new())
            };

            if content.is_empty() {
                if let Some(old_node_id) = old_node_id {
                    return Ok(old_node_id);
                }
            }

            let old_length = old_bytes.len();
            let mut new_bytes = Vec::with_capacity(old_length + content.len());
            new_bytes.extend_from_slice(&old_bytes);
            new_bytes.extend_from_slice(content);
            let payload = json!({
                "path": path.clone(),
                "ref_name": ref_name,
                "old_node_id": old_node_id.clone(),
                "append_length": content.len(),
                "old_length": old_length,
                "new_length": new_bytes.len(),
            })
            .to_string();
            let derived_from_nodes = old_node_id.into_iter().collect();
            let node_id = self.write_content_in_tx(
                &tx,
                &path,
                &new_bytes,
                derived_from_nodes,
                "append",
                payload,
                tool_call_id.as_deref(),
            )?;
            tx.commit()?;
            Ok(node_id)
        })
    }

    fn truncate_path_impl(
        &self,
        logical_path: &str,
        length: i64,
        tool_call_id: Option<String>,
    ) -> AnfsResult<String> {
        if length < 0 {
            return Err(AnfsError::PolicyDenied(
                "truncate length must be non-negative".to_string(),
            ));
        }
        let new_len = usize::try_from(length)
            .map_err(|_| AnfsError::PolicyDenied("truncate length is too large".to_string()))?;

        with_sqlite_busy_retry(|| {
            let path = normalize_logical_path(logical_path);
            if path.is_empty() || is_explicit_ref(logical_path) {
                return Err(AnfsError::PolicyDenied(
                    "truncate requires a workspace logical file path".to_string(),
                ));
            }
            let ref_name = workspace_ref_name(&self.workspace_name, &path);
            let mut conn = lock_conn(&self.inner)?;
            let tx = conn.transaction()?;
            let rec = if let Some(rec) = fetch_ref(&tx, &ref_name)? {
                rec
            } else {
                let dir_ref_name =
                    workspace_ref_name(&self.workspace_name, &normalize_dir_path(&path));
                if fetch_ref(&tx, &dir_ref_name)?.is_some() {
                    return Err(AnfsError::PolicyDenied(format!(
                        "cannot truncate directory path {logical_path}"
                    )));
                }
                return Err(AnfsError::RefNotFound(ref_name.clone()));
            };
            if is_directory_ref_name(&ref_name) || is_directory_node_tx(&tx, &rec.node_id)? {
                return Err(AnfsError::PolicyDenied(format!(
                    "cannot truncate directory path {logical_path}"
                )));
            }
            require_path_readable(self, &ref_name, &rec)?;
            ensure_node_fragments_visible(&tx, &rec.node_id, || {
                format!(
                    "truncate blocked by fragment policy label on source node {}",
                    rec.node_id
                )
            })?;

            let old_bytes = read_node_bytes(&tx, &self.inner.objects_dir, &rec.node_id)?;
            if new_len == old_bytes.len() {
                return Ok(rec.node_id);
            }
            let old_length = old_bytes.len();
            let mut new_bytes = old_bytes;
            new_bytes.resize(new_len, 0);
            let payload = json!({
                "path": path.clone(),
                "ref_name": ref_name,
                "old_node_id": rec.node_id.clone(),
                "old_length": old_length,
                "new_length": new_len,
            })
            .to_string();
            let node_id = self.write_content_in_tx(
                &tx,
                &path,
                &new_bytes,
                vec![rec.node_id],
                "truncate",
                payload,
                tool_call_id.as_deref(),
            )?;
            tx.commit()?;
            Ok(node_id)
        })
    }

    fn stat_impl(&self, logical_path: &str) -> AnfsResult<PosixStatRow> {
        let path = normalize_logical_path(logical_path);
        if path.is_empty() {
            return Ok((
                "".to_string(),
                "directory".to_string(),
                None,
                None,
                None,
                None,
                Some(DIRECTORY_MEDIA_TYPE.to_string()),
                0,
            ));
        }

        let ref_name = self.resolve_path_ref(&path);
        let conn = lock_conn(&self.inner)?;
        if let Some(row) = stat_ref_row(&conn, &path, &ref_name)? {
            return Ok(row);
        }

        let dir_prefix = normalize_dir_path(&path);
        let dir_ref_name = workspace_ref_name(&self.workspace_name, &dir_prefix);
        if let Some(row) = stat_ref_row(&conn, dir_prefix.trim_end_matches('/'), &dir_ref_name)? {
            return Ok(row);
        }

        let workspace_prefix = format!("{}/", self.workspace_name.trim_end_matches('/'));
        let like = format!("{workspace_prefix}{dir_prefix}%");
        let child_count: i64 = conn.query_row(
            "SELECT COUNT(*)
             FROM refs
             WHERE ref_name LIKE ?1
               AND state NOT IN ('deleted', 'archived')",
            params![like],
            |row| row.get(0),
        )?;
        if child_count > 0 {
            return Ok((
                dir_prefix.trim_end_matches('/').to_string(),
                "directory".to_string(),
                None,
                None,
                Some("draft".to_string()),
                None,
                Some(DIRECTORY_MEDIA_TYPE.to_string()),
                0,
            ));
        }

        Err(AnfsError::RefNotFound(ref_name))
    }

    fn path_kind_matches(&self, logical_path: &str, kind: Option<&str>) -> AnfsResult<bool> {
        match self.stat_impl(logical_path) {
            Ok(row) => Ok(match kind {
                Some(expected) => row.1 == expected,
                None => true,
            }),
            Err(AnfsError::RefNotFound(_)) => Ok(false),
            Err(err) => Err(err),
        }
    }

    fn stat_posix_impl(&self, logical_path: &str) -> AnfsResult<PosixStatDetailRow> {
        let row = self.stat_impl(logical_path)?;
        let conn = lock_conn(&self.inner)?;
        let timestamp_ms = posix_stat_timestamp_ms(&conn, &self.workspace_name, &row)?;
        let metadata = workspace_path_metadata(&conn, &self.workspace_name, &row.0)?;
        let mode = posix_stat_mode(&row.1, metadata.as_ref().and_then(|metadata| metadata.mode));
        let nlink = posix_stat_nlink(&conn, &self.workspace_name, &row)?;
        let inode_seed = posix_stat_inode_seed(&self.workspace_name, &row);
        let inode = stable_posix_inode(&inode_seed);
        let uid = metadata
            .as_ref()
            .and_then(|metadata| metadata.uid)
            .unwrap_or(0);
        let gid = metadata
            .as_ref()
            .and_then(|metadata| metadata.gid)
            .unwrap_or(0);
        let atime_ms = metadata
            .as_ref()
            .and_then(|metadata| metadata.atime_ms)
            .unwrap_or(timestamp_ms);
        let mtime_ms = metadata
            .as_ref()
            .and_then(|metadata| metadata.mtime_ms)
            .unwrap_or(timestamp_ms);
        let ctime_ms = metadata
            .as_ref()
            .and_then(|metadata| metadata.ctime_ms)
            .unwrap_or(timestamp_ms);
        Ok((
            row.0, row.1, row.2, row.3, row.4, row.5, row.6, row.7, mode, nlink, uid, gid, inode,
            atime_ms, mtime_ms, ctime_ms,
        ))
    }

    fn chmod_impl(
        &self,
        logical_path: &str,
        mode: i64,
        tool_call_id: Option<String>,
    ) -> AnfsResult<()> {
        if !(0..=0o7777).contains(&mode) {
            return Err(AnfsError::PolicyDenied(
                "chmod mode must be a POSIX permission mask from 0 to 0o7777".to_string(),
            ));
        }
        if is_explicit_ref(logical_path) {
            return Err(AnfsError::PolicyDenied(
                "chmod applies to workspace logical paths, not explicit refs".to_string(),
            ));
        }
        let row = self.stat_posix_impl(logical_path)?;
        let path = row.0.clone();
        with_sqlite_busy_retry(|| {
            let mut conn = lock_conn(&self.inner)?;
            let tx = conn.transaction()?;
            let event_id = new_event_id();
            let now = now_millis();
            let old_mode = row.8 & 0o7777;
            let metadata_paths =
                linked_metadata_paths(&tx, &self.workspace_name, &path, &row.1, row.3.as_deref())?;
            let payload = json!({
                "path": path,
                "kind": row.1,
                "old_mode": old_mode,
                "new_mode": mode,
                "affected_paths": metadata_paths,
            })
            .to_string();
            insert_event(
                &tx,
                &event_id,
                "chmod",
                Some(&self.agent_id),
                self.run_id.as_deref(),
                tool_call_id.as_deref(),
                Some(&self.workspace_name),
                Some(&payload),
            )?;
            if let Some(node_id) = row.3.as_deref() {
                insert_edge(&tx, &event_id, "input", node_id, "target", Some(&path))?;
            }
            for metadata_path in &metadata_paths {
                upsert_workspace_path_metadata(
                    &tx,
                    &self.workspace_name,
                    metadata_path,
                    &MetadataOverlayUpdate {
                        mode: Some(mode),
                        ctime_ms: Some(now),
                        ..Default::default()
                    },
                    &event_id,
                    now,
                    false,
                )?;
            }
            tx.commit()?;
            Ok(())
        })
    }

    fn chown_impl(
        &self,
        logical_path: &str,
        uid: i64,
        gid: i64,
        tool_call_id: Option<String>,
    ) -> AnfsResult<()> {
        if uid < -1 || gid < -1 {
            return Err(AnfsError::PolicyDenied(
                "chown uid and gid must be non-negative, or -1 to preserve the existing value"
                    .to_string(),
            ));
        }
        if uid == -1 && gid == -1 {
            return Err(AnfsError::PolicyDenied(
                "chown must change uid, gid, or both".to_string(),
            ));
        }
        if is_explicit_ref(logical_path) {
            return Err(AnfsError::PolicyDenied(
                "chown applies to workspace logical paths, not explicit refs".to_string(),
            ));
        }
        let row = self.stat_posix_impl(logical_path)?;
        let path = row.0.clone();
        with_sqlite_busy_retry(|| {
            let mut conn = lock_conn(&self.inner)?;
            let tx = conn.transaction()?;
            let event_id = new_event_id();
            let now = now_millis();
            let new_uid = if uid == -1 { row.10 } else { uid };
            let new_gid = if gid == -1 { row.11 } else { gid };
            let metadata_paths =
                linked_metadata_paths(&tx, &self.workspace_name, &path, &row.1, row.3.as_deref())?;
            let payload = json!({
                "path": path,
                "kind": row.1,
                "old_uid": row.10,
                "old_gid": row.11,
                "new_uid": new_uid,
                "new_gid": new_gid,
                "affected_paths": metadata_paths,
            })
            .to_string();
            insert_event(
                &tx,
                &event_id,
                "chown",
                Some(&self.agent_id),
                self.run_id.as_deref(),
                tool_call_id.as_deref(),
                Some(&self.workspace_name),
                Some(&payload),
            )?;
            if let Some(node_id) = row.3.as_deref() {
                insert_edge(&tx, &event_id, "input", node_id, "target", Some(&path))?;
            }
            for metadata_path in &metadata_paths {
                upsert_workspace_path_metadata(
                    &tx,
                    &self.workspace_name,
                    metadata_path,
                    &MetadataOverlayUpdate {
                        uid: Some(new_uid),
                        gid: Some(new_gid),
                        ctime_ms: Some(now),
                        ..Default::default()
                    },
                    &event_id,
                    now,
                    false,
                )?;
            }
            tx.commit()?;
            Ok(())
        })
    }

    fn utime_impl(
        &self,
        logical_path: &str,
        atime_ms: Option<i64>,
        mtime_ms: Option<i64>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<()> {
        if atime_ms.is_some_and(|value| value < 0) || mtime_ms.is_some_and(|value| value < 0) {
            return Err(AnfsError::PolicyDenied(
                "utime timestamps must be non-negative milliseconds".to_string(),
            ));
        }
        if is_explicit_ref(logical_path) {
            return Err(AnfsError::PolicyDenied(
                "utime applies to workspace logical paths, not explicit refs".to_string(),
            ));
        }
        let row = self.stat_posix_impl(logical_path)?;
        let path = row.0.clone();
        with_sqlite_busy_retry(|| {
            let mut conn = lock_conn(&self.inner)?;
            let tx = conn.transaction()?;
            let event_id = new_event_id();
            let now = now_millis();
            let new_atime_ms = if atime_ms.is_none() && mtime_ms.is_none() {
                now
            } else {
                atime_ms.unwrap_or(row.13)
            };
            let new_mtime_ms = if atime_ms.is_none() && mtime_ms.is_none() {
                now
            } else {
                mtime_ms.unwrap_or(row.14)
            };
            let metadata_paths =
                linked_metadata_paths(&tx, &self.workspace_name, &path, &row.1, row.3.as_deref())?;
            let payload = json!({
                "path": path,
                "kind": row.1,
                "old_atime_ms": row.13,
                "old_mtime_ms": row.14,
                "new_atime_ms": new_atime_ms,
                "new_mtime_ms": new_mtime_ms,
                "affected_paths": metadata_paths,
            })
            .to_string();
            insert_event(
                &tx,
                &event_id,
                "utime",
                Some(&self.agent_id),
                self.run_id.as_deref(),
                tool_call_id.as_deref(),
                Some(&self.workspace_name),
                Some(&payload),
            )?;
            if let Some(node_id) = row.3.as_deref() {
                insert_edge(&tx, &event_id, "input", node_id, "target", Some(&path))?;
            }
            for metadata_path in &metadata_paths {
                upsert_workspace_path_metadata(
                    &tx,
                    &self.workspace_name,
                    metadata_path,
                    &MetadataOverlayUpdate {
                        atime_ms: Some(new_atime_ms),
                        mtime_ms: Some(new_mtime_ms),
                        ctime_ms: Some(now),
                        ..Default::default()
                    },
                    &event_id,
                    now,
                    false,
                )?;
            }
            tx.commit()?;
            Ok(())
        })
    }

    fn access_impl(
        &self,
        logical_path: &str,
        mode: i64,
        uid: Option<i64>,
        gid: Option<i64>,
        groups: Vec<i64>,
    ) -> AnfsResult<bool> {
        if !(0..=7).contains(&mode) {
            return Err(AnfsError::PolicyDenied(
                "access mode must be an os.access-style mask from 0 to 7".to_string(),
            ));
        }
        if uid.is_some_and(|value| value < 0)
            || gid.is_some_and(|value| value < 0)
            || groups.iter().any(|group| *group < 0)
        {
            return Err(AnfsError::PolicyDenied(
                "access uid, gid, and groups must be non-negative when supplied".to_string(),
            ));
        }
        if uid.is_some() != gid.is_some() {
            return Err(AnfsError::PolicyDenied(
                "access uid and gid must be supplied together".to_string(),
            ));
        }
        let stat = match self.stat_posix_impl(logical_path) {
            Ok(stat) => stat,
            Err(AnfsError::RefNotFound(_)) => return Ok(false),
            Err(err) => return Err(err),
        };
        if mode == 0 {
            return Ok(true);
        }

        let path = normalize_logical_path(logical_path);
        if mode & 4 != 0 {
            if let (Some(ref_name), Some(node_id), Some(state), Some(ref_version)) =
                (&stat.2, &stat.3, &stat.4, stat.5)
            {
                let rec = RefRecord {
                    node_id: node_id.clone(),
                    ref_kind: "workspace".to_string(),
                    state: state.clone(),
                    ref_version,
                };
                if require_path_readable(self, ref_name, &rec).is_err() {
                    return Ok(false);
                }
                let conn = lock_conn(&self.inner)?;
                if node_fragments_hidden(&conn, node_id)? {
                    return Ok(false);
                }
            }
            if posix_access_permission_bits(stat.8, stat.10, stat.11, uid, gid, &groups) & 4 == 0 {
                return Ok(false);
            }
        }
        if mode & 2 != 0
            && (is_explicit_ref(logical_path)
                || (!path.is_empty()
                    && posix_access_permission_bits(stat.8, stat.10, stat.11, uid, gid, &groups)
                        & 2
                        == 0)
                || (path.is_empty()
                    && posix_access_permission_bits(stat.8, stat.10, stat.11, uid, gid, &groups)
                        & 2
                        == 0))
            {
                return Ok(false);
            }
        if mode & 1 != 0
            && posix_access_permission_bits(stat.8, stat.10, stat.11, uid, gid, &groups) & 1 == 0
        {
            return Ok(false);
        }

        Ok(true)
    }

    fn ls_impl(&self, logical_path: &str) -> AnfsResult<Vec<PosixLsRow>> {
        let dir_prefix = normalize_dir_path(logical_path);
        let workspace_prefix = format!("{}/", self.workspace_name.trim_end_matches('/'));
        let full_prefix = format!("{workspace_prefix}{dir_prefix}");
        let like = format!("{full_prefix}%");
        let conn = lock_conn(&self.inner)?;
        let mut stmt = conn.prepare(
            "SELECT r.ref_name,
                    r.node_id,
                    r.state,
                    r.ref_version,
                    COALESCE(n.kind, ''),
                    n.media_type,
                    b.size
             FROM refs r
             JOIN nodes n ON n.node_id = r.node_id
             JOIN blobs b ON b.hash = n.blob_hash
             WHERE r.ref_name LIKE ?1
               AND r.state NOT IN ('deleted', 'archived')
             ORDER BY r.ref_name",
        )?;
        let rows = stmt.query_map(params![like], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, i64>(3)?,
                row.get::<_, String>(4)?,
                row.get::<_, Option<String>>(5)?,
                row.get::<_, i64>(6)?,
            ))
        })?;

        let mut entries: BTreeMap<String, PosixLsRow> = BTreeMap::new();
        for row in rows {
            let (ref_name, node_id, state, ref_version, node_kind, media_type, size) = row?;
            let Some(suffix) = ref_name.strip_prefix(&full_prefix) else {
                continue;
            };
            if suffix.is_empty() {
                continue;
            }
            let (name, is_dir) = next_path_segment(suffix);
            if is_dir {
                entries.entry(name.clone()).or_insert((
                    name,
                    "directory".to_string(),
                    None,
                    None,
                    Some("draft".to_string()),
                    None,
                    0,
                ));
                continue;
            }
            let kind = if is_directory_kind(&node_kind, media_type.as_deref()) {
                "directory"
            } else {
                "file"
            };
            entries.insert(
                name.clone(),
                (
                    name,
                    kind.to_string(),
                    Some(ref_name),
                    Some(node_id),
                    Some(state),
                    Some(ref_version),
                    size,
                ),
            );
        }
        Ok(entries.into_values().collect())
    }

    fn mkdir_impl(&self, logical_path: &str, tool_call_id: Option<String>) -> AnfsResult<String> {
        let dir_path = normalize_dir_path(logical_path);
        if dir_path.is_empty() {
            return Err(AnfsError::PolicyDenied(
                "cannot create workspace root directory".to_string(),
            ));
        }
        let ref_name = workspace_ref_name(&self.workspace_name, &dir_path);
        let content = br#"{"type":"directory"}"#;
        let hash = sha256_hex(content);
        let blob_storage = materialize_blob(&self.inner.objects_dir, &hash, content)?;

        let mut conn = lock_conn(&self.inner)?;
        let tx = conn.transaction()?;
        require_path_unlocked_for_mutation(&tx, self, &dir_path, false, now_millis())?;
        let old_ref = fetch_ref(&tx, &ref_name)?;
        let created_new_path = old_ref
            .as_ref()
            .is_none_or(|old| old.state == "deleted" || old.state == "archived");
        if let Some(existing) = old_ref {
            if existing.state != "deleted" && existing.state != "archived" {
                return Err(AnfsError::RefConflict(format!(
                    "directory ref {ref_name} already exists"
                )));
            }
        }

        insert_blob(&tx, &hash, content.len() as i64, &blob_storage)?;
        let node_id = new_node_id();
        tx.execute(
            "INSERT INTO nodes (node_id, blob_hash, kind, media_type, metadata_json, created_at)
             VALUES (?1, ?2, 'directory', ?3, ?4, ?5)",
            params![
                node_id,
                hash,
                DIRECTORY_MEDIA_TYPE,
                json!({"directory": true}).to_string(),
                now_millis()
            ],
        )?;

        let event_id = new_event_id();
        insert_event(
            &tx,
            &event_id,
            "write",
            Some(&self.agent_id),
            self.run_id.as_deref(),
            tool_call_id.as_deref(),
            Some(&self.workspace_name),
            Some(&dir_path),
        )?;
        insert_edge(
            &tx,
            &event_id,
            "output",
            &node_id,
            "result",
            Some(&dir_path),
        )?;
        upsert_ref(
            &tx,
            &ref_name,
            &node_id,
            "workspace",
            "draft",
            &event_id,
            RefWriteMode::WorkspaceDraft,
        )?;
        if created_new_path {
            inherit_parent_setgid_metadata(
                &tx,
                &self.workspace_name,
                &dir_path,
                "directory",
                &event_id,
            )?;
        }
        tx.commit()?;
        Ok(dir_path)
    }

    fn touch_impl(&self, logical_path: &str, tool_call_id: Option<String>) -> AnfsResult<String> {
        let path = normalize_logical_path(logical_path);
        if path.is_empty() {
            return Err(AnfsError::PolicyDenied(
                "cannot touch workspace root directory".to_string(),
            ));
        }
        let ref_name = workspace_ref_name(&self.workspace_name, &path);
        {
            let conn = lock_conn(&self.inner)?;
            if let Some(existing) = fetch_ref(&conn, &ref_name)? {
                if existing.state != "deleted" && existing.state != "archived" {
                    if is_directory_ref_name(&ref_name)
                        || is_directory_node_tx(&conn, &existing.node_id)?
                    {
                        return Err(AnfsError::PolicyDenied(format!(
                            "cannot touch directory path {path}"
                        )));
                    }
                    return Ok(existing.node_id);
                }
            }
            let dir_path = normalize_dir_path(&path);
            let dir_ref_name = workspace_ref_name(&self.workspace_name, &dir_path);
            if let Some(existing_dir) = fetch_ref(&conn, &dir_ref_name)? {
                if existing_dir.state != "deleted" && existing_dir.state != "archived" {
                    return Err(AnfsError::PolicyDenied(format!(
                        "cannot touch directory path {path}"
                    )));
                }
            }
            let workspace_prefix = format!("{}/", self.workspace_name.trim_end_matches('/'));
            let like = format!("{workspace_prefix}{dir_path}%");
            let child_count: i64 = conn.query_row(
                "SELECT COUNT(*)
                 FROM refs
                 WHERE ref_name LIKE ?1
                   AND state NOT IN ('deleted', 'archived')",
                params![like],
                |row| row.get(0),
            )?;
            if child_count > 0 {
                return Err(AnfsError::PolicyDenied(format!(
                    "cannot touch directory path {path}"
                )));
            }
        }
        self.write_impl(&path, b"", Vec::new(), tool_call_id)
    }

    fn acquire_lock_impl(
        &self,
        logical_path: &str,
        ttl_ms: Option<i64>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<String> {
        if is_explicit_ref(logical_path) {
            return Err(AnfsError::PolicyDenied(
                "locks apply to workspace logical paths, not explicit refs".to_string(),
            ));
        }
        let ttl_ms = ttl_ms.unwrap_or(300_000);
        if ttl_ms <= 0 {
            return Err(AnfsError::PolicyDenied(
                "lock ttl_ms must be positive".to_string(),
            ));
        }
        let path = normalize_logical_path(logical_path);
        let mut conn = lock_conn(&self.inner)?;
        let tx = conn.transaction()?;
        let now = now_millis();
        cleanup_expired_workspace_locks(&tx, now)?;
        require_path_unlocked_for_mutation(&tx, self, &path, true, now)?;

        let lock_id = new_lock_id();
        let expires_at_ms = now
            .checked_add(ttl_ms)
            .ok_or_else(|| AnfsError::PolicyDenied("lock ttl_ms is too large".to_string()))?;
        let event_id = new_event_id();
        let payload = json!({
            "path": path,
            "lock_id": lock_id,
            "mode": "exclusive",
            "ttl_ms": ttl_ms,
            "expires_at_ms": expires_at_ms,
        })
        .to_string();
        insert_event(
            &tx,
            &event_id,
            "acquire_lock",
            Some(&self.agent_id),
            self.run_id.as_deref(),
            tool_call_id.as_deref(),
            Some(&self.workspace_name),
            Some(&payload),
        )?;
        tx.execute(
            "INSERT INTO workspace_path_locks
             (workspace_id, logical_path, lock_id, mode, owner_agent_id,
              owner_run_id, tool_call_id, acquired_at_ms, expires_at_ms, event_id)
             VALUES (?1, ?2, ?3, 'exclusive', ?4, ?5, ?6, ?7, ?8, ?9)
             ON CONFLICT(workspace_id, logical_path) DO UPDATE SET
                 lock_id = excluded.lock_id,
                 mode = excluded.mode,
                 owner_agent_id = excluded.owner_agent_id,
                 owner_run_id = excluded.owner_run_id,
                 tool_call_id = excluded.tool_call_id,
                 acquired_at_ms = excluded.acquired_at_ms,
                 expires_at_ms = excluded.expires_at_ms,
                 event_id = excluded.event_id",
            params![
                self.workspace_name.as_str(),
                path,
                lock_id,
                self.agent_id.as_str(),
                self.run_id.as_deref(),
                tool_call_id.as_deref(),
                now,
                expires_at_ms,
                event_id
            ],
        )?;
        tx.commit()?;
        Ok(lock_id)
    }

    fn release_lock_impl(
        &self,
        logical_path: &str,
        lock_id: &str,
        tool_call_id: Option<String>,
    ) -> AnfsResult<bool> {
        if lock_id.trim().is_empty() {
            return Err(AnfsError::PolicyDenied(
                "lock_id must not be empty".to_string(),
            ));
        }
        let path = normalize_logical_path(logical_path);
        let mut conn = lock_conn(&self.inner)?;
        let tx = conn.transaction()?;
        let now = now_millis();
        cleanup_expired_workspace_locks(&tx, now)?;
        let row: Option<(String, String, Option<String>, i64, Option<i64>)> = tx
            .query_row(
                "SELECT lock_id, owner_agent_id, owner_run_id, acquired_at_ms, expires_at_ms
                 FROM workspace_path_locks
                 WHERE workspace_id = ?1 AND logical_path = ?2",
                params![self.workspace_name.as_str(), path],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                    ))
                },
            )
            .optional()?;
        let Some((stored_lock_id, owner_agent_id, owner_run_id, acquired_at_ms, expires_at_ms)) =
            row
        else {
            tx.commit()?;
            return Ok(false);
        };
        if stored_lock_id != lock_id {
            tx.commit()?;
            return Ok(false);
        }
        if !workspace_lock_owner_matches(self, &owner_agent_id, owner_run_id.as_deref()) {
            return Err(AnfsError::PolicyDenied(format!(
                "lock {lock_id} on {path} is owned by another agent or run"
            )));
        }

        let event_id = new_event_id();
        let payload = json!({
            "path": path,
            "lock_id": lock_id,
            "mode": "exclusive",
            "acquired_at_ms": acquired_at_ms,
            "expires_at_ms": expires_at_ms,
        })
        .to_string();
        insert_event(
            &tx,
            &event_id,
            "release_lock",
            Some(&self.agent_id),
            self.run_id.as_deref(),
            tool_call_id.as_deref(),
            Some(&self.workspace_name),
            Some(&payload),
        )?;
        tx.execute(
            "DELETE FROM workspace_path_locks
             WHERE workspace_id = ?1 AND logical_path = ?2 AND lock_id = ?3",
            params![self.workspace_name.as_str(), path, lock_id],
        )?;
        tx.commit()?;
        Ok(true)
    }

    fn lock_info_impl(&self, logical_path: &str) -> AnfsResult<Vec<WorkspaceLockRow>> {
        let path = normalize_logical_path(logical_path);
        let mut conn = lock_conn(&self.inner)?;
        let tx = conn.transaction()?;
        let now = now_millis();
        cleanup_expired_workspace_locks(&tx, now)?;
        let rows = {
            let mut stmt = tx.prepare(
                "SELECT logical_path, lock_id, mode, owner_agent_id, owner_run_id,
                        acquired_at_ms, expires_at_ms
                 FROM workspace_path_locks
                 WHERE workspace_id = ?1
                   AND (?2 = '' OR logical_path = ?2 OR logical_path LIKE ?3)
                 ORDER BY logical_path",
            )?;
            let like = format!("{}%", normalize_dir_path(&path));
            let mapped =
                stmt.query_map(params![self.workspace_name.as_str(), path, like], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, String>(3)?,
                        row.get::<_, Option<String>>(4)?,
                        row.get::<_, i64>(5)?,
                        row.get::<_, Option<i64>>(6)?,
                    ))
                })?;
            mapped.collect::<Result<Vec<_>, _>>()?
        };
        tx.commit()?;
        Ok(rows)
    }

    pub(crate) fn delete_impl(
        &self,
        logical_path: &str,
        tool_call_id: Option<String>,
        uid: Option<i64>,
        gid: Option<i64>,
        groups: Vec<i64>,
    ) -> AnfsResult<()> {
        validate_posix_effective_identity(uid, gid, &groups)?;
        let mut conn = lock_conn(&self.inner)?;
        let tx = conn.transaction()?;
        self.delete_in_tx(
            &tx,
            logical_path,
            tool_call_id.as_deref(),
            uid,
            gid,
            &groups,
        )?;
        tx.commit()?;
        Ok(())
    }

    pub(crate) fn delete_in_tx(
        &self,
        tx: &Transaction<'_>,
        logical_path: &str,
        tool_call_id: Option<&str>,
        uid: Option<i64>,
        gid: Option<i64>,
        groups: &[i64],
    ) -> AnfsResult<()> {
        let logical_path = normalize_logical_path(logical_path);
        if logical_path.is_empty() {
            return Err(AnfsError::PolicyDenied(
                "cannot delete workspace root directory".to_string(),
            ));
        }
        require_path_unlocked_for_mutation(tx, self, &logical_path, true, now_millis())?;
        let ref_name = workspace_ref_name(&self.workspace_name, &logical_path);
        if let Some(old) = fetch_ref(tx, &ref_name)? {
            if old.state != "deleted" && old.state != "archived" {
                if is_directory_node_tx(tx, &old.node_id)? {
                    let entries =
                        workspace_subtree_entries(tx, &self.workspace_name, &logical_path)?;
                    delete_workspace_entries(tx, self, entries, tool_call_id, uid, gid, groups)?;
                } else {
                    delete_workspace_ref(
                        tx,
                        self,
                        &ref_name,
                        &old,
                        &logical_path,
                        tool_call_id,
                        uid,
                        gid,
                        groups,
                    )?;
                }
                return Ok(());
            }
        }

        let entries = workspace_subtree_entries(tx, &self.workspace_name, &logical_path)?;
        if entries.is_empty() {
            return Err(AnfsError::RefNotFound(ref_name));
        }
        delete_workspace_entries(tx, self, entries, tool_call_id, uid, gid, groups)?;
        Ok(())
    }

    fn cp_impl(
        &self,
        src_path: &str,
        dst_path: &str,
        tool_call_id: Option<String>,
    ) -> AnfsResult<String> {
        let src_ref = self.resolve_path_ref(src_path);
        let mut dst_path = normalize_logical_path(dst_path);
        if dst_path.is_empty() {
            return Err(AnfsError::PolicyDenied(
                "copy destination must not be workspace root".to_string(),
            ));
        }
        let mut conn = lock_conn(&self.inner)?;
        let tx = conn.transaction()?;

        if let Some(src) = fetch_ref(&tx, &src_ref)? {
            require_path_readable(self, &src_ref, &src)?;
            if !is_directory_node_tx(&tx, &src.node_id)? {
                dst_path = resolve_destination_path_for_existing_directory(
                    &tx,
                    &self.workspace_name,
                    src_path,
                    &dst_path,
                )?;
                let dst_ref = workspace_ref_name(&self.workspace_name, &dst_path);
                copy_workspace_ref(
                    &tx,
                    self,
                    &src_ref,
                    &src.node_id,
                    &dst_ref,
                    &dst_path,
                    tool_call_id.as_deref(),
                )?;
                tx.commit()?;
                return Ok(src.node_id);
            }
        }

        let src_dir = normalize_logical_path(src_path);
        dst_path = resolve_destination_path_for_existing_directory(
            &tx,
            &self.workspace_name,
            &src_dir,
            &dst_path,
        )?;
        let entries = workspace_subtree_entries(&tx, &self.workspace_name, &src_dir)?;
        if entries.is_empty() {
            return Err(AnfsError::RefNotFound(src_ref));
        }
        let root_node = copy_workspace_subtree(
            &tx,
            self,
            &src_dir,
            &dst_path,
            entries,
            tool_call_id.as_deref(),
        )?;
        tx.commit()?;
        Ok(root_node)
    }

    fn link_impl(
        &self,
        src_path: &str,
        dst_path: &str,
        tool_call_id: Option<String>,
    ) -> AnfsResult<String> {
        if is_explicit_ref(dst_path) {
            return Err(AnfsError::PolicyDenied(
                "link destination must be a workspace logical path".to_string(),
            ));
        }
        let dst_path = normalize_logical_path(dst_path);
        if dst_path.is_empty() {
            return Err(AnfsError::PolicyDenied(
                "link destination must not be workspace root".to_string(),
            ));
        }
        let src_stat = self.stat_impl(src_path)?;
        if src_stat.1 == "directory" {
            return Err(AnfsError::PolicyDenied(
                "link source must be a regular file".to_string(),
            ));
        }
        let src_ref = src_stat
            .2
            .clone()
            .ok_or_else(|| AnfsError::RefNotFound(normalize_logical_path(src_path)))?;
        let dst_ref = workspace_ref_name(&self.workspace_name, &dst_path);
        let dst_dir_ref = workspace_ref_name(&self.workspace_name, &normalize_dir_path(&dst_path));

        let mut conn = lock_conn(&self.inner)?;
        let tx = conn.transaction()?;
        let src =
            require_ref(&tx, &src_ref)?;
        require_path_readable(self, &src_ref, &src)?;
        if is_directory_node_tx(&tx, &src.node_id)? {
            return Err(AnfsError::PolicyDenied(
                "link source must be a regular file".to_string(),
            ));
        }
        if let Some(existing) = fetch_ref(&tx, &dst_ref)? {
            if existing.state != "deleted" && existing.state != "archived" {
                return Err(AnfsError::RefConflict(format!(
                    "link destination {dst_path} already exists"
                )));
            }
        }
        if let Some(existing_dir) = fetch_ref(&tx, &dst_dir_ref)? {
            if existing_dir.state != "deleted" && existing_dir.state != "archived" {
                return Err(AnfsError::RefConflict(format!(
                    "link destination {dst_path} is an existing directory"
                )));
            }
        }
        let workspace_prefix = format!("{}/", self.workspace_name.trim_end_matches('/'));
        let dst_dir_prefix = normalize_dir_path(&dst_path);
        let like = format!("{workspace_prefix}{dst_dir_prefix}%");
        let child_count: i64 = tx.query_row(
            "SELECT COUNT(*)
             FROM refs
             WHERE ref_name LIKE ?1
               AND state NOT IN ('deleted', 'archived')",
            params![like],
            |row| row.get(0),
        )?;
        if child_count > 0 {
            return Err(AnfsError::RefConflict(format!(
                "link destination {dst_path} is an existing directory"
            )));
        }

        link_workspace_ref(
            &tx,
            self,
            &src_ref,
            &src.node_id,
            &dst_ref,
            &dst_path,
            tool_call_id.as_deref(),
        )?;
        tx.commit()?;
        Ok(src.node_id)
    }

    fn mv_impl(
        &self,
        src_path: &str,
        dst_path: &str,
        tool_call_id: Option<String>,
        uid: Option<i64>,
        gid: Option<i64>,
        groups: Vec<i64>,
    ) -> AnfsResult<String> {
        validate_posix_effective_identity(uid, gid, &groups)?;
        let src_path = normalize_logical_path(src_path);
        let mut dst_path = normalize_logical_path(dst_path);
        if src_path.is_empty() || dst_path.is_empty() {
            return Err(AnfsError::PolicyDenied(
                "mv source and destination must not be workspace root".to_string(),
            ));
        }
        let src_ref = workspace_ref_name(&self.workspace_name, &src_path);
        let mut conn = lock_conn(&self.inner)?;
        let tx = conn.transaction()?;

        if let Some(src) = fetch_ref(&tx, &src_ref)? {
            require_path_readable(self, &src_ref, &src)?;
            if !is_directory_node_tx(&tx, &src.node_id)? {
                dst_path = resolve_destination_path_for_existing_directory(
                    &tx,
                    &self.workspace_name,
                    &src_path,
                    &dst_path,
                )?;
                let dst_ref = workspace_ref_name(&self.workspace_name, &dst_path);
                require_sticky_entry_mutation_allowed(
                    &tx,
                    &self.workspace_name,
                    &src_path,
                    &src.node_id,
                    uid,
                )?;
                require_sticky_destination_replacement_allowed(
                    &tx,
                    &self.workspace_name,
                    &dst_ref,
                    &dst_path,
                    uid,
                )?;
                copy_workspace_ref(
                    &tx,
                    self,
                    &src_ref,
                    &src.node_id,
                    &dst_ref,
                    &dst_path,
                    tool_call_id.as_deref(),
                )?;
                delete_workspace_ref(
                    &tx,
                    self,
                    &src_ref,
                    &src,
                    &src_path,
                    tool_call_id.as_deref(),
                    uid,
                    gid,
                    &groups,
                )?;
                tx.commit()?;
                return Ok(src.node_id);
            }
        }

        let src_dir = normalize_logical_path(&src_path);
        dst_path = resolve_destination_path_for_existing_directory(
            &tx,
            &self.workspace_name,
            &src_dir,
            &dst_path,
        )?;
        let src_dir_prefix = normalize_dir_path(&src_dir);
        let dst_dir_prefix = normalize_dir_path(&dst_path);
        if dst_dir_prefix.starts_with(&src_dir_prefix) {
            return Err(AnfsError::PolicyDenied(format!(
                "cannot move directory {src_path} into its own subtree {dst_path}"
            )));
        }
        let entries = workspace_subtree_entries(&tx, &self.workspace_name, &src_dir)?;
        if entries.is_empty() {
            return Err(AnfsError::RefNotFound(src_ref));
        }
        let root_node = move_workspace_subtree(
            &tx,
            self,
            &src_dir,
            &dst_path,
            entries,
            tool_call_id.as_deref(),
            uid,
            gid,
            &groups,
        )?;
        tx.commit()?;
        Ok(root_node)
    }

    fn find_impl(
        &self,
        logical_path: &str,
        pattern: Option<&str>,
        tool_call_id: Option<String>,
        limit: Option<usize>,
    ) -> AnfsResult<Vec<PosixFindRow>> {
        let dir_prefix = normalize_dir_path(logical_path);
        let workspace_prefix = format!("{}/", self.workspace_name.trim_end_matches('/'));
        let full_prefix = format!("{workspace_prefix}{dir_prefix}");
        let like = format!("{full_prefix}%");
        let conn = lock_conn(&self.inner)?;
        let mut stmt = conn.prepare(
            "SELECT r.ref_name, r.node_id
             FROM refs r
             WHERE r.ref_name LIKE ?1
               AND r.state NOT IN ('deleted', 'archived')
             ORDER BY r.ref_name",
        )?;
        let rows = stmt.query_map(params![like], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })?;
        let mut results = Vec::new();
        let mut event_nodes = Vec::new();
        for row in rows {
            let (ref_name, node_id) = row?;
            let Some(path) = workspace_logical_path(&self.workspace_name, &ref_name) else {
                continue;
            };
            let match_target = path
                .trim_end_matches('/')
                .rsplit('/')
                .next()
                .unwrap_or(&path);
            if pattern.is_some_and(|pat| !wildcard_match(pat, match_target)) {
                continue;
            }
            let kind = if is_directory_ref_name(&ref_name) {
                "directory"
            } else {
                "file"
            };
            event_nodes.push((ref_name.clone(), node_id.clone()));
            results.push((path, kind.to_string(), ref_name, node_id));
            if limit.is_some_and(|limit| results.len() >= limit) {
                break;
            }
        }
        drop(stmt);
        drop(conn);
        self.record_posix_search_event(
            "find",
            pattern.unwrap_or("*"),
            &dir_prefix,
            event_nodes,
            tool_call_id,
        )?;
        Ok(results)
    }

    fn grep_impl(
        &self,
        pattern: &str,
        logical_path: &str,
        tool_call_id: Option<String>,
        limit: Option<usize>,
    ) -> AnfsResult<Vec<PosixGrepRow>> {
        let dir_prefix = normalize_dir_path(logical_path);
        let workspace_prefix = format!("{}/", self.workspace_name.trim_end_matches('/'));
        let full_prefix = format!("{workspace_prefix}{dir_prefix}");
        let like = format!("{full_prefix}%");
        let conn = lock_conn(&self.inner)?;
        let mut stmt = conn.prepare(
            "SELECT r.ref_name,
                    r.node_id,
                    COALESCE(n.kind, ''),
                    n.media_type,
                    b.storage_kind,
                    b.inline_content
             FROM refs r
             JOIN nodes n ON n.node_id = r.node_id
             JOIN blobs b ON b.hash = n.blob_hash
             WHERE r.ref_name LIKE ?1
               AND r.state NOT IN ('deleted', 'archived')
             ORDER BY r.ref_name",
        )?;
        let rows = stmt.query_map(params![like], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, Option<String>>(3)?,
                row.get::<_, String>(4)?,
                row.get::<_, Option<Vec<u8>>>(5)?,
            ))
        })?;

        let mut candidates = Vec::new();
        for row in rows {
            let (ref_name, node_id, node_kind, media_type, storage_kind, inline_content) = row?;
            if is_directory_kind(&node_kind, media_type.as_deref()) {
                continue;
            }
            if let Some(path) = workspace_logical_path(&self.workspace_name, &ref_name) {
                candidates.push((path, ref_name, node_id, storage_kind, inline_content));
            }
        }
        drop(stmt);
        drop(conn);

        let mut results = Vec::new();
        let mut event_nodes = Vec::new();
        let mut seen_nodes = BTreeSet::new();
        for (path, ref_name, node_id, storage_kind, inline_content) in candidates {
            let text;
            let owned_text;
            if storage_kind == "inline" {
                let bytes = inline_content.ok_or_else(|| {
                    AnfsError::StorageCorruption(format!(
                        "inline node {node_id} has no inline content"
                    ))
                })?;
                owned_text = String::from_utf8_lossy(&bytes).into_owned();
                text = owned_text.as_str();
            } else {
                let bytes = {
                    let conn = lock_conn(&self.inner)?;
                    read_node_bytes(&conn, &self.inner.objects_dir, &node_id)?
                };
                owned_text = String::from_utf8_lossy(&bytes).into_owned();
                text = owned_text.as_str();
            }
            for (idx, line) in text.lines().enumerate() {
                if line.contains(pattern) {
                    if seen_nodes.insert(node_id.clone()) {
                        event_nodes.push((ref_name.clone(), node_id.clone()));
                    }
                    results.push((
                        path.clone(),
                        (idx + 1) as i64,
                        line.to_string(),
                        node_id.clone(),
                        ref_name.clone(),
                    ));
                    if limit.is_some_and(|limit| results.len() >= limit) {
                        break;
                    }
                }
            }
            if limit.is_some_and(|limit| results.len() >= limit) {
                break;
            }
        }
        self.record_posix_search_event("grep", pattern, &dir_prefix, event_nodes, tool_call_id)?;
        Ok(results)
    }

    fn record_posix_search_event(
        &self,
        tool: &str,
        query: &str,
        logical_path: &str,
        result_nodes: Vec<(String, String)>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<()> {
        with_sqlite_busy_retry(|| {
            let mut conn = lock_conn(&self.inner)?;
            let tx = conn.transaction()?;
            let event_id = new_event_id();
            let payload = json!({
                "tool": tool,
                "query": query,
                "path": logical_path,
                "scope": "workspace",
                "result_count": result_nodes.len(),
            })
            .to_string();
            insert_event(
                &tx,
                &event_id,
                "search",
                Some(&self.agent_id),
                self.run_id.as_deref(),
                tool_call_id.as_deref(),
                Some(&self.workspace_name),
                Some(&payload),
            )?;
            for (idx, (ref_name, node_id)) in result_nodes.iter().enumerate() {
                let role = format!("search_result:{idx}");
                insert_edge(&tx, &event_id, "input", node_id, &role, Some(ref_name))?;
            }
            tx.commit()?;
            Ok(())
        })
    }

    fn resolve_path_ref(&self, logical_path: &str) -> String {
        if is_explicit_ref(logical_path) {
            logical_path.to_string()
        } else {
            workspace_ref_name(&self.workspace_name, &normalize_logical_path(logical_path))
        }
    }

    fn search_impl(
        &self,
        query: &str,
        scope: &str,
        tool_call_id: Option<String>,
        policy_label_excludes: Vec<String>,
        purpose: Option<String>,
    ) -> AnfsResult<Vec<(String, String)>> {
        let visibility = VisibilityPolicy::new(&policy_label_excludes)?;
        let state_filter = match scope {
            "published" => "published",
            "approved" => "approved",
            "draft" => "draft",
            _ => {
                return Err(AnfsError::PolicyDenied(format!(
                    "unsupported search scope {scope}"
                )))
            }
        };

        let mut conn = lock_conn(&self.inner)?;
        let tx = conn.transaction()?;
        if let Some(purpose_value) = purpose.as_deref() {
            self.require_purpose_capability(&tx, purpose_value, "search")?;
        }
        let fts_query = fts_literal_query(query)?;
        let mut results = Vec::new();
        let mut seen = HashSet::new();
        {
            let workspace_like = format!("{}/%", self.workspace_name.trim_end_matches('/'));
            let unrestricted = visibility.is_unrestricted_for(&tx)?;
            let sql_limit = if unrestricted && purpose.is_none() {
                100
            } else {
                1000
            };
            let mut stmt = tx.prepare(
                "SELECT f.node_id,
                        snippet(node_fts, 1, '[', ']', '...', 12),
                        r.ref_name,
                        r.state,
                        r.ref_version
             FROM node_fts f
             JOIN refs r ON r.node_id = f.node_id
             WHERE node_fts MATCH ?1 AND r.state = ?2
               AND (?3 IS NULL OR r.ref_name LIKE ?3)
             ORDER BY r.ref_name
             LIMIT ?4",
            )?;
            let draft_workspace_filter = if scope == "draft" {
                Some(workspace_like.as_str())
            } else {
                None
            };
            let rows = stmt.query_map(
                params![
                    fts_query.as_str(),
                    state_filter,
                    draft_workspace_filter,
                    sql_limit
                ],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, String>(3)?,
                        row.get::<_, i64>(4)?,
                    ))
                },
            )?;

            for row in rows {
                let (node_id, snippet, ref_name, state, ref_version) = row?;
                if visibility.ref_node_blocked(&tx, &ref_name, &node_id)? {
                    continue;
                }
                if let Some(purpose_value) = purpose.as_deref() {
                    if purpose_hides_ref_node(&tx, purpose_value, &ref_name, &node_id)? {
                        continue;
                    }
                }
                if seen.insert(node_id.clone()) {
                    results.push((node_id, snippet, ref_name, state, ref_version));
                    if results.len() == 20 {
                        break;
                    }
                }
            }
        }

        let event_id = new_event_id();
        let result_payload: Vec<_> = results
            .iter()
            .map(|(node_id, _snippet, ref_name, state, ref_version)| {
                json!({
                    "ref_name": ref_name,
                    "node_id": node_id,
                    "state": state,
                    "ref_version": ref_version,
                })
            })
            .collect();
        let payload = json!({
            "query": query,
            "scope": scope,
            "policy_label_excludes": policy_label_excludes,
            "purpose": purpose,
            "result_count": results.len(),
            "results": result_payload,
        })
        .to_string();
        insert_event(
            &tx,
            &event_id,
            "search",
            Some(&self.agent_id),
            self.run_id.as_deref(),
            tool_call_id.as_deref(),
            Some(&self.workspace_name),
            Some(&payload),
        )?;
        for (idx, (node_id, _snippet, ref_name, _state, _ref_version)) in results.iter().enumerate()
        {
            let role = format!("search_result:{idx}");
            insert_edge(&tx, &event_id, "input", node_id, &role, Some(ref_name))?;
        }
        tx.commit()?;

        Ok(results
            .into_iter()
            .map(|(node_id, snippet, _ref_name, _state, _ref_version)| (node_id, snippet))
            .collect())
    }

    fn query_impl(
        &self,
        prefix: Option<String>,
        text: Option<String>,
        state: Option<String>,
        agent_id: Option<String>,
        run_id: Option<String>,
        event_kind: Option<String>,
        media_type: Option<String>,
        created_after_ms: Option<i64>,
        created_before_ms: Option<i64>,
        policy: Option<String>,
        decision: Option<String>,
        reason_code: Option<String>,
        limit: i64,
        tool_call_id: Option<String>,
        policy_label_excludes: Vec<String>,
        purpose: Option<String>,
    ) -> AnfsResult<Vec<QueryRefRow>> {
        let mut conn = lock_conn(&self.inner)?;
        let tx = conn.transaction()?;
        if let Some(purpose_value) = purpose.as_deref() {
            self.require_purpose_capability(&tx, purpose_value, "query")?;
        }
        let results = query_refs(
            &tx,
            prefix.as_deref(),
            text.as_deref(),
            state.as_deref(),
            agent_id.as_deref(),
            run_id.as_deref(),
            event_kind.as_deref(),
            media_type.as_deref(),
            created_after_ms,
            created_before_ms,
            policy.as_deref(),
            decision.as_deref(),
            reason_code.as_deref(),
            &policy_label_excludes,
            purpose.as_deref(),
            limit,
        )?;

        let event_id = new_event_id();
        let result_payload: Vec<_> = results
            .iter()
            .map(
                |(
                    ref_name,
                    node_id,
                    ref_kind,
                    state,
                    ref_version,
                    node_kind,
                    media_type,
                    node_created_at,
                    last_event_seq,
                    last_event_kind,
                    snippet,
                )| {
                    json!({
                        "ref_name": ref_name,
                        "node_id": node_id,
                        "ref_kind": ref_kind,
                        "state": state,
                        "ref_version": ref_version,
                        "node_kind": node_kind,
                        "media_type": media_type,
                        "node_created_at": node_created_at,
                        "last_event_seq": last_event_seq,
                        "last_event_kind": last_event_kind,
                        "snippet": snippet,
                    })
                },
            )
            .collect();
        let payload = json!({
            "prefix": prefix,
            "text": text,
            "state": state,
            "agent_id": agent_id,
            "run_id": run_id,
            "event_kind": event_kind,
            "media_type": media_type,
            "created_after_ms": created_after_ms,
            "created_before_ms": created_before_ms,
            "policy": policy,
            "decision": decision,
            "reason_code": reason_code,
            "limit": limit,
            "policy_label_excludes": policy_label_excludes,
            "purpose": purpose,
            "result_count": results.len(),
            "results": result_payload,
        })
        .to_string();
        insert_event(
            &tx,
            &event_id,
            "query",
            Some(&self.agent_id),
            self.run_id.as_deref(),
            tool_call_id.as_deref(),
            Some(&self.workspace_name),
            Some(&payload),
        )?;
        for (idx, (ref_name, node_id, ..)) in results.iter().enumerate() {
            let role = format!("query_result:{idx}");
            insert_edge(&tx, &event_id, "input", node_id, &role, Some(ref_name))?;
        }
        tx.commit()?;
        Ok(results)
    }

    fn require_purpose_capability(
        &self,
        conn: &Connection,
        purpose: &str,
        operation: &str,
    ) -> AnfsResult<()> {
        if let Some(capability) = active_purpose_capability_missing(conn, purpose, &self.agent_id)?
        {
            return Err(AnfsError::PolicyDenied(format!(
                "{operation} for purpose {purpose} requires capability {capability} for agent {}",
                self.agent_id
            )));
        }
        Ok(())
    }
}

fn normalize_logical_path(path: &str) -> String {
    path.split('/')
        .filter(|segment| !segment.is_empty() && *segment != ".")
        .collect::<Vec<_>>()
        .join("/")
}

fn normalize_dir_path(path: &str) -> String {
    let path = normalize_logical_path(path);
    if path.is_empty() {
        path
    } else {
        format!("{path}/")
    }
}

fn path_basename(path: &str) -> AnfsResult<String> {
    let normalized = normalize_logical_path(path);
    normalized
        .rsplit('/')
        .next()
        .filter(|segment| !segment.is_empty())
        .map(|segment| segment.to_string())
        .ok_or_else(|| AnfsError::PolicyDenied("source path must not be empty".to_string()))
}

fn join_logical_path(parent: &str, child: &str) -> String {
    if parent.is_empty() {
        child.to_string()
    } else {
        format!("{parent}/{child}")
    }
}

fn resolve_destination_path_for_existing_directory(
    conn: &Connection,
    workspace: &str,
    src_path: &str,
    dst_path: &str,
) -> AnfsResult<String> {
    if workspace_path_is_active_directory_tx(conn, workspace, dst_path)? {
        Ok(join_logical_path(dst_path, &path_basename(src_path)?))
    } else {
        Ok(dst_path.to_string())
    }
}

fn estimated_token_count(byte_count: i64) -> i64 {
    if byte_count <= 0 {
        0
    } else {
        (byte_count + 3) / 4
    }
}

fn is_explicit_ref(path: &str) -> bool {
    path.starts_with("ws:")
        || path.starts_with("artifact:")
        || path.starts_with("resource:")
        || path.starts_with("memory:")
}

fn is_directory_ref_name(ref_name: &str) -> bool {
    ref_name.ends_with('/')
}

fn is_directory_kind(node_kind: &str, media_type: Option<&str>) -> bool {
    node_kind == "directory" || media_type == Some(DIRECTORY_MEDIA_TYPE)
}

fn next_path_segment(suffix: &str) -> (String, bool) {
    let mut parts = suffix.splitn(2, '/');
    let first = parts.next().unwrap_or_default().to_string();
    let is_dir = parts.next().is_some();
    (first, is_dir)
}

fn wildcard_match(pattern: &str, text: &str) -> bool {
    let pattern = pattern.as_bytes();
    let text = text.as_bytes();
    let mut dp = vec![vec![false; text.len() + 1]; pattern.len() + 1];
    dp[0][0] = true;
    for i in 1..=pattern.len() {
        if pattern[i - 1] == b'*' {
            dp[i][0] = dp[i - 1][0];
        }
    }
    for i in 1..=pattern.len() {
        for j in 1..=text.len() {
            dp[i][j] = match pattern[i - 1] {
                b'*' => dp[i - 1][j] || dp[i][j - 1],
                b'?' => dp[i - 1][j - 1],
                ch => ch == text[j - 1] && dp[i - 1][j - 1],
            };
        }
    }
    dp[pattern.len()][text.len()]
}

fn stat_ref_row(
    conn: &Connection,
    logical_path: &str,
    ref_name: &str,
) -> AnfsResult<Option<PosixStatRow>> {
    let mut stmt = conn.prepare(
        "SELECT r.ref_name,
                r.node_id,
                r.state,
                r.ref_version,
                COALESCE(n.kind, ''),
                n.media_type,
                b.size
         FROM refs r
         JOIN nodes n ON n.node_id = r.node_id
         JOIN blobs b ON b.hash = n.blob_hash
         WHERE r.ref_name = ?1
           AND r.state NOT IN ('deleted', 'archived')",
    )?;
    let mut rows = stmt.query(params![ref_name])?;
    let Some(row) = rows.next()? else {
        return Ok(None);
    };
    let ref_name = row.get::<_, String>(0)?;
    let node_id = row.get::<_, String>(1)?;
    let state = row.get::<_, String>(2)?;
    let ref_version = row.get::<_, i64>(3)?;
    let node_kind = row.get::<_, String>(4)?;
    let media_type = row.get::<_, Option<String>>(5)?;
    let size = row.get::<_, i64>(6)?;
    let kind = if is_directory_kind(&node_kind, media_type.as_deref()) {
        "directory"
    } else {
        "file"
    };
    Ok(Some((
        logical_path.to_string(),
        kind.to_string(),
        Some(ref_name),
        Some(node_id),
        Some(state),
        Some(ref_version),
        media_type,
        size,
    )))
}

fn posix_stat_mode(kind: &str, permissions: Option<i64>) -> i64 {
    posix_stat_file_type_bits(kind) | permissions.unwrap_or_else(|| posix_default_permissions(kind))
}

fn posix_stat_file_type_bits(kind: &str) -> i64 {
    match kind {
        "directory" => 0o040000,
        _ => 0o100000,
    }
}

fn posix_default_permissions(kind: &str) -> i64 {
    match kind {
        "directory" => 0o755,
        _ => 0o644,
    }
}

fn workspace_path_metadata(
    conn: &Connection,
    workspace_name: &str,
    logical_path: &str,
) -> AnfsResult<Option<WorkspacePathMetadata>> {
    let metadata = conn
        .query_row(
            "SELECT mode, uid, gid, atime_ms, mtime_ms, ctime_ms
             FROM workspace_path_metadata
             WHERE workspace_id = ?1 AND logical_path = ?2",
            params![workspace_name, logical_path],
            |row| {
                Ok(WorkspacePathMetadata {
                    mode: row.get(0)?,
                    uid: row.get(1)?,
                    gid: row.get(2)?,
                    atime_ms: row.get(3)?,
                    mtime_ms: row.get(4)?,
                    ctime_ms: row.get(5)?,
                })
            },
        )
        .optional()?;
    Ok(metadata)
}

fn validate_posix_effective_identity(
    uid: Option<i64>,
    gid: Option<i64>,
    groups: &[i64],
) -> AnfsResult<()> {
    if uid.is_some_and(|value| value < 0)
        || gid.is_some_and(|value| value < 0)
        || groups.iter().any(|group| *group < 0)
    {
        return Err(AnfsError::PolicyDenied(
            "effective uid, gid, and groups must be non-negative when supplied".to_string(),
        ));
    }
    if uid.is_some() != gid.is_some() {
        return Err(AnfsError::PolicyDenied(
            "effective uid and gid must be supplied together".to_string(),
        ));
    }
    Ok(())
}

fn workspace_parent_path(logical_path: &str) -> String {
    let path = normalize_logical_path(logical_path);
    path.rsplit_once('/')
        .map(|(parent, _child)| parent.to_string())
        .unwrap_or_default()
}

fn require_sticky_destination_replacement_allowed(
    conn: &Connection,
    workspace_name: &str,
    dst_ref: &str,
    dst_logical_path: &str,
    uid: Option<i64>,
) -> AnfsResult<()> {
    let Some(existing) = fetch_ref(conn, dst_ref)? else {
        return Ok(());
    };
    if existing.state == "deleted" || existing.state == "archived" {
        return Ok(());
    }
    require_sticky_entry_mutation_allowed(
        conn,
        workspace_name,
        dst_logical_path,
        &existing.node_id,
        uid,
    )
}

fn require_sticky_entry_mutation_allowed(
    conn: &Connection,
    workspace_name: &str,
    logical_path: &str,
    node_id: &str,
    uid: Option<i64>,
) -> AnfsResult<()> {
    let Some(uid) = uid else {
        return Ok(());
    };
    if uid == 0 {
        return Ok(());
    }
    let parent_path = workspace_parent_path(logical_path);
    let parent_metadata = workspace_path_metadata(conn, workspace_name, &parent_path)?;
    let parent_mode = posix_stat_mode(
        "directory",
        parent_metadata.as_ref().and_then(|metadata| metadata.mode),
    );
    if parent_mode & 0o1000 == 0 {
        return Ok(());
    }
    let parent_uid = parent_metadata
        .as_ref()
        .and_then(|metadata| metadata.uid)
        .unwrap_or(0);
    let entry_metadata = workspace_path_metadata(conn, workspace_name, logical_path)?;
    let entry_uid = entry_metadata
        .as_ref()
        .and_then(|metadata| metadata.uid)
        .unwrap_or(0);
    if uid == parent_uid || uid == entry_uid {
        return Ok(());
    }
    Err(AnfsError::PolicyDenied(format!(
        "sticky directory {} blocks mutation of {logical_path} by uid {uid} for node {node_id}",
        if parent_path.is_empty() {
            "<workspace-root>"
        } else {
            parent_path.as_str()
        }
    )))
}

fn cleanup_expired_workspace_locks(conn: &Connection, now_ms: i64) -> AnfsResult<()> {
    conn.execute(
        "DELETE FROM workspace_path_locks
         WHERE expires_at_ms IS NOT NULL AND expires_at_ms <= ?1",
        params![now_ms],
    )?;
    Ok(())
}

fn workspace_lock_owner_matches(
    workspace: &Workspace,
    owner_agent_id: &str,
    owner_run_id: Option<&str>,
) -> bool {
    owner_agent_id == workspace.agent_id && owner_run_id == workspace.run_id.as_deref()
}

fn workspace_lock_path_conflicts(
    locked_path: &str,
    target_path: &str,
    include_descendants: bool,
) -> bool {
    if locked_path == target_path || locked_path.is_empty() {
        return true;
    }
    let locked_prefix = normalize_dir_path(locked_path);
    if target_path.starts_with(&locked_prefix) {
        return true;
    }
    if include_descendants {
        if target_path.is_empty() {
            return true;
        }
        let target_prefix = normalize_dir_path(target_path);
        return locked_path.starts_with(&target_prefix);
    }
    false
}

fn require_path_unlocked_for_mutation(
    conn: &Connection,
    workspace: &Workspace,
    logical_path: &str,
    include_descendants: bool,
    now_ms: i64,
) -> AnfsResult<()> {
    cleanup_expired_workspace_locks(conn, now_ms)?;
    let target_path = normalize_logical_path(logical_path);
    let mut stmt = conn.prepare(
        "SELECT logical_path, lock_id, owner_agent_id, owner_run_id, expires_at_ms
         FROM workspace_path_locks
         WHERE workspace_id = ?1
           AND (expires_at_ms IS NULL OR expires_at_ms > ?2)
         ORDER BY logical_path",
    )?;
    let rows = stmt.query_map(params![workspace.workspace_name.as_str(), now_ms], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, Option<String>>(3)?,
            row.get::<_, Option<i64>>(4)?,
        ))
    })?;
    for row in rows {
        let (locked_path, lock_id, owner_agent_id, owner_run_id, expires_at_ms) = row?;
        if workspace_lock_owner_matches(workspace, &owner_agent_id, owner_run_id.as_deref()) {
            continue;
        }
        if workspace_lock_path_conflicts(&locked_path, &target_path, include_descendants) {
            return Err(AnfsError::PolicyDenied(format!(
                "path {target_path} is blocked by exclusive lock {lock_id} on {locked_path} owned by {owner_agent_id} until {}",
                expires_at_ms
                    .map(|value| value.to_string())
                    .unwrap_or_else(|| "no expiry".to_string())
            )));
        }
    }
    Ok(())
}

/// POSIX metadata overlay update for a workspace logical path. `None` fields
/// are left untouched on existing rows (and NULL on fresh rows) unless
/// `overwrite_all` is set, which copies every field verbatim.
#[derive(Default)]
struct MetadataOverlayUpdate {
    mode: Option<i64>,
    uid: Option<i64>,
    gid: Option<i64>,
    atime_ms: Option<i64>,
    mtime_ms: Option<i64>,
    ctime_ms: Option<i64>,
}

fn upsert_workspace_path_metadata(
    conn: &Connection,
    workspace_name: &str,
    logical_path: &str,
    update: &MetadataOverlayUpdate,
    event_id: &str,
    updated_at: i64,
    overwrite_all: bool,
) -> AnfsResult<()> {
    let sql = if overwrite_all {
        "INSERT INTO workspace_path_metadata
         (workspace_id, logical_path, mode, uid, gid, atime_ms, mtime_ms, ctime_ms, event_id, updated_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)
         ON CONFLICT(workspace_id, logical_path) DO UPDATE SET
             mode = excluded.mode,
             uid = excluded.uid,
             gid = excluded.gid,
             atime_ms = excluded.atime_ms,
             mtime_ms = excluded.mtime_ms,
             ctime_ms = excluded.ctime_ms,
             event_id = excluded.event_id,
             updated_at = excluded.updated_at"
    } else {
        "INSERT INTO workspace_path_metadata
         (workspace_id, logical_path, mode, uid, gid, atime_ms, mtime_ms, ctime_ms, event_id, updated_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)
         ON CONFLICT(workspace_id, logical_path) DO UPDATE SET
             mode = COALESCE(excluded.mode, workspace_path_metadata.mode),
             uid = COALESCE(excluded.uid, workspace_path_metadata.uid),
             gid = COALESCE(excluded.gid, workspace_path_metadata.gid),
             atime_ms = COALESCE(excluded.atime_ms, workspace_path_metadata.atime_ms),
             mtime_ms = COALESCE(excluded.mtime_ms, workspace_path_metadata.mtime_ms),
             ctime_ms = COALESCE(excluded.ctime_ms, workspace_path_metadata.ctime_ms),
             event_id = excluded.event_id,
             updated_at = excluded.updated_at"
    };
    conn.execute(
        sql,
        params![
            workspace_name,
            logical_path,
            update.mode,
            update.uid,
            update.gid,
            update.atime_ms,
            update.mtime_ms,
            update.ctime_ms,
            event_id,
            updated_at
        ],
    )?;
    Ok(())
}

fn inherit_parent_setgid_metadata(
    conn: &Connection,
    workspace_name: &str,
    logical_path: &str,
    kind: &str,
    event_id: &str,
) -> AnfsResult<()> {
    let parent_path = workspace_parent_path(logical_path);
    let Some(parent_metadata) = workspace_path_metadata(conn, workspace_name, &parent_path)? else {
        return Ok(());
    };
    let parent_mode = posix_stat_mode("directory", parent_metadata.mode);
    if parent_mode & 0o2000 == 0 {
        return Ok(());
    }
    let inherited_gid = parent_metadata.gid.unwrap_or(0);
    let inherited_mode = if kind == "directory" {
        Some(posix_default_permissions("directory") | 0o2000)
    } else {
        None
    };
    let now = now_millis();
    upsert_workspace_path_metadata(
        conn,
        workspace_name,
        &normalize_logical_path(logical_path),
        &MetadataOverlayUpdate {
            mode: inherited_mode,
            gid: Some(inherited_gid),
            ctime_ms: Some(now),
            ..Default::default()
        },
        event_id,
        now,
        false,
    )?;
    Ok(())
}

fn linked_metadata_paths(
    conn: &Connection,
    workspace_name: &str,
    logical_path: &str,
    kind: &str,
    node_id: Option<&str>,
) -> AnfsResult<Vec<String>> {
    let Some(node_id) = node_id else {
        return Ok(vec![logical_path.to_string()]);
    };
    if kind != "file" {
        return Ok(vec![logical_path.to_string()]);
    }
    let workspace_prefix = format!("{}/", workspace_name.trim_end_matches('/'));
    let mut stmt = conn.prepare(
        "SELECT r.ref_name
         FROM refs r
         JOIN nodes n ON n.node_id = r.node_id
         WHERE r.ref_name LIKE ?1
           AND r.node_id = ?2
           AND r.state NOT IN ('deleted', 'archived')
           AND COALESCE(n.kind, '') != 'directory'
           AND (n.media_type IS NULL OR n.media_type != ?3)
         ORDER BY r.ref_name",
    )?;
    let rows = stmt.query_map(
        params![
            format!("{workspace_prefix}%"),
            node_id,
            DIRECTORY_MEDIA_TYPE
        ],
        |row| row.get::<_, String>(0),
    )?;
    let mut paths = BTreeSet::new();
    paths.insert(logical_path.to_string());
    for row in rows {
        let ref_name = row?;
        if let Some(path) = workspace_logical_path(workspace_name, &ref_name) {
            paths.insert(path);
        }
    }
    Ok(paths.into_iter().collect())
}

fn copy_workspace_metadata_overlay(
    conn: &Connection,
    workspace_name: &str,
    source_ref: &str,
    dst_logical_path: &str,
    event_id: &str,
) -> AnfsResult<()> {
    let Some(source_path) = workspace_logical_path(workspace_name, source_ref) else {
        return Ok(());
    };
    let Some(metadata) = workspace_path_metadata(conn, workspace_name, &source_path)? else {
        return Ok(());
    };
    upsert_workspace_path_metadata(
        conn,
        workspace_name,
        dst_logical_path,
        &MetadataOverlayUpdate {
            mode: metadata.mode,
            uid: metadata.uid,
            gid: metadata.gid,
            atime_ms: metadata.atime_ms,
            mtime_ms: metadata.mtime_ms,
            ctime_ms: metadata.ctime_ms,
        },
        event_id,
        now_millis(),
        true,
    )?;
    Ok(())
}

fn stable_posix_inode(seed: &str) -> i64 {
    let digest = sha256_hex(seed.as_bytes());
    i64::from_str_radix(&digest[..15], 16).unwrap_or(1).max(1)
}

fn posix_stat_nlink(
    conn: &Connection,
    workspace_name: &str,
    row: &PosixStatRow,
) -> AnfsResult<i64> {
    if row.1 == "directory" {
        return Ok(2);
    }
    let Some(ref_name) = row.2.as_deref() else {
        return Ok(1);
    };
    let workspace_prefix = format!("{}/", workspace_name.trim_end_matches('/'));
    if !ref_name.starts_with(&workspace_prefix) {
        return Ok(1);
    }
    let Some(node_id) = row.3.as_deref() else {
        return Ok(1);
    };
    let count: i64 = conn.query_row(
        "SELECT COUNT(*)
         FROM refs r
         JOIN nodes n ON n.node_id = r.node_id
         WHERE r.ref_name LIKE ?1
           AND r.node_id = ?2
           AND r.state NOT IN ('deleted', 'archived')
           AND COALESCE(n.kind, '') != 'directory'
           AND (n.media_type IS NULL OR n.media_type != ?3)",
        params![
            format!("{workspace_prefix}%"),
            node_id,
            DIRECTORY_MEDIA_TYPE
        ],
        |record| record.get(0),
    )?;
    Ok(count.max(1))
}

fn posix_stat_inode_seed(workspace_name: &str, row: &PosixStatRow) -> String {
    if row.1 == "file" {
        if let (Some(ref_name), Some(node_id)) = (row.2.as_deref(), row.3.as_deref()) {
            let workspace_prefix = format!("{}/", workspace_name.trim_end_matches('/'));
            if ref_name.starts_with(&workspace_prefix) {
                return node_id.to_string();
            }
        }
    }
    row.2
        .clone()
        .unwrap_or_else(|| workspace_ref_name(workspace_name, &row.0))
}

fn posix_access_permission_bits(
    stat_mode: i64,
    stat_uid: i64,
    stat_gid: i64,
    effective_uid: Option<i64>,
    effective_gid: Option<i64>,
    groups: &[i64],
) -> i64 {
    let permission_mode = stat_mode & 0o777;
    let Some(effective_uid) = effective_uid else {
        return (permission_mode >> 6) & 0o7;
    };
    if effective_uid == 0 {
        let mut bits = 0o6;
        if permission_mode & 0o111 != 0 {
            bits |= 0o1;
        }
        return bits;
    }
    if effective_uid == stat_uid {
        return (permission_mode >> 6) & 0o7;
    }
    let effective_gid = effective_gid.unwrap_or(-1);
    if effective_gid == stat_gid || groups.contains(&stat_gid) {
        return (permission_mode >> 3) & 0o7;
    }
    permission_mode & 0o7
}

fn posix_stat_timestamp_ms(
    conn: &Connection,
    workspace_name: &str,
    row: &PosixStatRow,
) -> AnfsResult<i64> {
    if let Some(ref_name) = row.2.as_deref() {
        let updated_at: Option<i64> = conn
            .query_row(
                "SELECT updated_at FROM refs WHERE ref_name = ?1",
                params![ref_name],
                |record| record.get(0),
            )
            .optional()?;
        return Ok(updated_at.unwrap_or(0));
    }

    let prefix = if row.0.is_empty() {
        format!("{}/%", workspace_name.trim_end_matches('/'))
    } else {
        let dir_prefix = normalize_dir_path(&row.0);
        format!("{}/{}%", workspace_name.trim_end_matches('/'), dir_prefix)
    };
    let updated_at: Option<i64> = conn.query_row(
        "SELECT MAX(updated_at)
         FROM refs
         WHERE ref_name LIKE ?1
           AND state NOT IN ('deleted', 'archived')",
        params![prefix],
        |record| record.get(0),
    )?;
    Ok(updated_at.unwrap_or(0))
}

fn is_directory_node(inner: &Inner, node_id: &str) -> AnfsResult<bool> {
    let conn = lock_conn(inner)?;
    is_directory_node_tx(&conn, node_id)
}

fn is_directory_node_tx(conn: &Connection, node_id: &str) -> AnfsResult<bool> {
    let row: Option<(String, Option<String>)> = conn
        .query_row(
            "SELECT COALESCE(kind, ''), media_type FROM nodes WHERE node_id = ?1",
            params![node_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()?;
    let Some((kind, media_type)) = row else {
        return Err(AnfsError::NodeNotFound(node_id.to_string()));
    };
    Ok(is_directory_kind(&kind, media_type.as_deref()))
}

fn workspace_path_is_active_directory_tx(
    conn: &Connection,
    workspace: &str,
    logical_path: &str,
) -> AnfsResult<bool> {
    let path = normalize_logical_path(logical_path);
    if path.is_empty() {
        return Ok(true);
    }

    let ref_name = workspace_ref_name(workspace, &path);
    if let Some(row) = fetch_ref(conn, &ref_name)? {
        if row.state != "deleted" && row.state != "archived" {
            return is_directory_node_tx(conn, &row.node_id);
        }
    }

    let dir_prefix = normalize_dir_path(&path);
    let dir_ref_name = workspace_ref_name(workspace, &dir_prefix);
    if let Some(row) = fetch_ref(conn, &dir_ref_name)? {
        if row.state != "deleted" && row.state != "archived" {
            return is_directory_node_tx(conn, &row.node_id);
        }
    }

    let workspace_prefix = format!("{}/", workspace.trim_end_matches('/'));
    let like = format!("{workspace_prefix}{dir_prefix}%");
    let child_count: i64 = conn.query_row(
        "SELECT COUNT(*)
         FROM refs
         WHERE ref_name LIKE ?1
           AND state NOT IN ('deleted', 'archived')",
        params![like],
        |row| row.get(0),
    )?;
    Ok(child_count > 0)
}

fn workspace_subtree_entries(
    conn: &Connection,
    workspace: &str,
    logical_path: &str,
) -> AnfsResult<Vec<WorkspaceRefEntry>> {
    let dir_prefix = normalize_dir_path(logical_path);
    let workspace_prefix = format!("{}/", workspace.trim_end_matches('/'));
    let marker_ref = workspace_ref_name(workspace, &dir_prefix);
    let like = format!("{workspace_prefix}{dir_prefix}%");
    let mut stmt = conn.prepare(
        "SELECT r.ref_name,
                r.node_id,
                r.state,
                r.ref_version
         FROM refs r
         WHERE (r.ref_name = ?1 OR r.ref_name LIKE ?2)
           AND r.state NOT IN ('deleted', 'archived')
         ORDER BY r.ref_name",
    )?;
    let rows = stmt.query_map(params![marker_ref, like], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, i64>(3)?,
        ))
    })?;
    let mut entries = Vec::new();
    for row in rows {
        let (ref_name, node_id, state, ref_version) = row?;
        let Some(logical_path) = workspace_logical_path(workspace, &ref_name) else {
            continue;
        };
        entries.push(WorkspaceRefEntry {
            ref_name,
            logical_path,
            node_id,
            state,
            ref_version,
        });
    }
    Ok(entries)
}

fn copy_workspace_subtree(
    tx: &Transaction<'_>,
    workspace: &Workspace,
    src_dir: &str,
    dst_dir: &str,
    entries: Vec<WorkspaceRefEntry>,
    tool_call_id: Option<&str>,
) -> AnfsResult<String> {
    let src_prefix = normalize_dir_path(src_dir);
    let dst_prefix = normalize_dir_path(dst_dir);
    let mut root_node = None;
    let mut copied_root_marker = false;

    for entry in entries {
        require_path_readable(workspace, &entry.ref_name, &entry.as_ref_record())?;
        let Some(suffix) = entry.logical_path.strip_prefix(&src_prefix) else {
            continue;
        };
        let dst_logical_path = if suffix.is_empty() {
            copied_root_marker = true;
            dst_prefix.clone()
        } else {
            format!("{dst_prefix}{suffix}")
        };
        let dst_ref = workspace_ref_name(&workspace.workspace_name, &dst_logical_path);
        require_path_unlocked_for_mutation(tx, workspace, &dst_logical_path, false, now_millis())?;
        copy_workspace_ref(
            tx,
            workspace,
            &entry.ref_name,
            &entry.node_id,
            &dst_ref,
            &dst_logical_path,
            tool_call_id,
        )?;
        if suffix.is_empty() {
            root_node = Some(entry.node_id);
        }
    }

    if !copied_root_marker {
        let marker_node = write_directory_marker_ref(tx, workspace, &dst_prefix, tool_call_id)?;
        root_node = Some(marker_node);
    }

    root_node.ok_or_else(|| {
        AnfsError::RefNotFound(format!(
            "workspace subtree {} has no copyable refs",
            src_prefix.trim_end_matches('/')
        ))
    })
}

struct WorkspaceMoveEntry {
    source: WorkspaceRefEntry,
    dst_ref: String,
    dst_logical_path: String,
}

fn move_workspace_subtree(
    tx: &Transaction<'_>,
    workspace: &Workspace,
    src_dir: &str,
    dst_dir: &str,
    entries: Vec<WorkspaceRefEntry>,
    tool_call_id: Option<&str>,
    uid: Option<i64>,
    _gid: Option<i64>,
    _groups: &[i64],
) -> AnfsResult<String> {
    let src_prefix = normalize_dir_path(src_dir);
    let dst_prefix = normalize_dir_path(dst_dir);
    let mut root_node = None;
    let mut moved_root_marker = false;
    let mut moved_entries = Vec::with_capacity(entries.len());

    for entry in entries {
        require_path_readable(workspace, &entry.ref_name, &entry.as_ref_record())?;
        require_path_unlocked_for_mutation(
            tx,
            workspace,
            &entry.logical_path,
            false,
            now_millis(),
        )?;
        require_sticky_entry_mutation_allowed(
            tx,
            &workspace.workspace_name,
            &entry.logical_path,
            &entry.node_id,
            uid,
        )?;
        let Some(suffix) = entry.logical_path.strip_prefix(&src_prefix) else {
            continue;
        };
        let dst_logical_path = if suffix.is_empty() {
            moved_root_marker = true;
            dst_prefix.clone()
        } else {
            format!("{dst_prefix}{suffix}")
        };
        let dst_ref = workspace_ref_name(&workspace.workspace_name, &dst_logical_path);
        require_path_unlocked_for_mutation(tx, workspace, &dst_logical_path, false, now_millis())?;
        require_sticky_destination_replacement_allowed(
            tx,
            &workspace.workspace_name,
            &dst_ref,
            &dst_logical_path,
            uid,
        )?;
        if suffix.is_empty() {
            root_node = Some(entry.node_id.clone());
        }
        moved_entries.push(WorkspaceMoveEntry {
            source: entry,
            dst_ref,
            dst_logical_path,
        });
    }

    if !moved_root_marker {
        let marker_node = write_directory_marker_ref(tx, workspace, &dst_prefix, tool_call_id)?;
        root_node = Some(marker_node);
    }
    if moved_entries.is_empty() {
        return root_node.ok_or_else(|| {
            AnfsError::RefNotFound(format!(
                "workspace subtree {} has no movable refs",
                src_prefix.trim_end_matches('/')
            ))
        });
    }

    let write_event_id = new_event_id();
    insert_event(
        tx,
        &write_event_id,
        "write",
        Some(&workspace.agent_id),
        workspace.run_id.as_deref(),
        tool_call_id,
        Some(&workspace.workspace_name),
        Some(&dst_prefix),
    )?;
    for (idx, moved) in moved_entries.iter().enumerate() {
        let input_role = format!("source:{idx}");
        let output_role = format!("result:{idx}");
        insert_edge(
            tx,
            &write_event_id,
            "input",
            &moved.source.node_id,
            &input_role,
            Some(&moved.source.ref_name),
        )?;
        insert_edge(
            tx,
            &write_event_id,
            "output",
            &moved.source.node_id,
            &output_role,
            Some(&moved.dst_logical_path),
        )?;
        upsert_ref(
            tx,
            &moved.dst_ref,
            &moved.source.node_id,
            "workspace",
            "draft",
            &write_event_id,
            RefWriteMode::WorkspaceDraft,
        )?;
    }

    let delete_event_id = new_event_id();
    insert_event(
        tx,
        &delete_event_id,
        "delete_ref",
        Some(&workspace.agent_id),
        workspace.run_id.as_deref(),
        tool_call_id,
        Some(&workspace.workspace_name),
        Some(&src_prefix),
    )?;
    for (idx, moved) in moved_entries.iter().enumerate() {
        let role = format!("deleted:{idx}");
        insert_edge(
            tx,
            &delete_event_id,
            "input",
            &moved.source.node_id,
            &role,
            Some(&moved.source.logical_path),
        )?;
        update_ref_state(
            tx,
            &moved.source.ref_name,
            &moved.source.as_ref_record(),
            "deleted",
            &delete_event_id,
        )?;
    }

    root_node.ok_or_else(|| {
        AnfsError::RefNotFound(format!(
            "workspace subtree {} has no movable root",
            src_prefix.trim_end_matches('/')
        ))
    })
}

fn copy_workspace_ref(
    tx: &Transaction<'_>,
    workspace: &Workspace,
    source_ref: &str,
    source_node_id: &str,
    dst_ref: &str,
    dst_logical_path: &str,
    tool_call_id: Option<&str>,
) -> AnfsResult<()> {
    require_path_unlocked_for_mutation(tx, workspace, dst_logical_path, false, now_millis())?;
    let event_id = new_event_id();
    insert_event(
        tx,
        &event_id,
        "write",
        Some(&workspace.agent_id),
        workspace.run_id.as_deref(),
        tool_call_id,
        Some(&workspace.workspace_name),
        Some(dst_logical_path),
    )?;
    insert_edge(
        tx,
        &event_id,
        "input",
        source_node_id,
        "source",
        Some(source_ref),
    )?;
    insert_edge(
        tx,
        &event_id,
        "output",
        source_node_id,
        "result",
        Some(dst_logical_path),
    )?;
    upsert_ref(
        tx,
        dst_ref,
        source_node_id,
        "workspace",
        "draft",
        &event_id,
        RefWriteMode::WorkspaceDraft,
    )
}

fn link_workspace_ref(
    tx: &Transaction<'_>,
    workspace: &Workspace,
    source_ref: &str,
    source_node_id: &str,
    dst_ref: &str,
    dst_logical_path: &str,
    tool_call_id: Option<&str>,
) -> AnfsResult<()> {
    require_path_unlocked_for_mutation(tx, workspace, dst_logical_path, false, now_millis())?;
    let event_id = new_event_id();
    let payload = json!({
        "source_ref": source_ref,
        "destination_ref": dst_ref,
        "destination_path": dst_logical_path,
        "node_id": source_node_id,
    })
    .to_string();
    insert_event(
        tx,
        &event_id,
        "link",
        Some(&workspace.agent_id),
        workspace.run_id.as_deref(),
        tool_call_id,
        Some(&workspace.workspace_name),
        Some(&payload),
    )?;
    insert_edge(
        tx,
        &event_id,
        "input",
        source_node_id,
        "source",
        Some(source_ref),
    )?;
    insert_edge(
        tx,
        &event_id,
        "output",
        source_node_id,
        "result",
        Some(dst_logical_path),
    )?;
    upsert_ref(
        tx,
        dst_ref,
        source_node_id,
        "workspace",
        "draft",
        &event_id,
        RefWriteMode::WorkspaceDraft,
    )?;
    copy_workspace_metadata_overlay(
        tx,
        &workspace.workspace_name,
        source_ref,
        dst_logical_path,
        &event_id,
    )
}

fn write_directory_marker_ref(
    tx: &Transaction<'_>,
    workspace: &Workspace,
    logical_path: &str,
    tool_call_id: Option<&str>,
) -> AnfsResult<String> {
    let dir_path = normalize_dir_path(logical_path);
    require_path_unlocked_for_mutation(tx, workspace, &dir_path, false, now_millis())?;
    let content = br#"{"type":"directory"}"#;
    let hash = sha256_hex(content);
    let blob_storage = materialize_blob(&workspace.inner.objects_dir, &hash, content)?;
    insert_blob(tx, &hash, content.len() as i64, &blob_storage)?;

    let node_id = new_node_id();
    tx.execute(
        "INSERT INTO nodes (node_id, blob_hash, kind, media_type, metadata_json, created_at)
         VALUES (?1, ?2, 'directory', ?3, ?4, ?5)",
        params![
            node_id,
            hash,
            DIRECTORY_MEDIA_TYPE,
            json!({"directory": true}).to_string(),
            now_millis()
        ],
    )?;

    let event_id = new_event_id();
    insert_event(
        tx,
        &event_id,
        "write",
        Some(&workspace.agent_id),
        workspace.run_id.as_deref(),
        tool_call_id,
        Some(&workspace.workspace_name),
        Some(&dir_path),
    )?;
    insert_edge(tx, &event_id, "output", &node_id, "result", Some(&dir_path))?;
    let ref_name = workspace_ref_name(&workspace.workspace_name, &dir_path);
    upsert_ref(
        tx,
        &ref_name,
        &node_id,
        "workspace",
        "draft",
        &event_id,
        RefWriteMode::WorkspaceDraft,
    )?;
    inherit_parent_setgid_metadata(
        tx,
        &workspace.workspace_name,
        &dir_path,
        "directory",
        &event_id,
    )?;
    Ok(node_id)
}

fn delete_workspace_entries(
    tx: &Transaction<'_>,
    workspace: &Workspace,
    mut entries: Vec<WorkspaceRefEntry>,
    tool_call_id: Option<&str>,
    uid: Option<i64>,
    gid: Option<i64>,
    groups: &[i64],
) -> AnfsResult<()> {
    entries.sort_by(|left, right| right.logical_path.cmp(&left.logical_path));
    for entry in entries {
        delete_workspace_ref(
            tx,
            workspace,
            &entry.ref_name,
            &entry.as_ref_record(),
            &entry.logical_path,
            tool_call_id,
            uid,
            gid,
            groups,
        )?;
    }
    Ok(())
}

fn delete_workspace_ref(
    tx: &Transaction<'_>,
    workspace: &Workspace,
    ref_name: &str,
    old: &RefRecord,
    logical_path: &str,
    tool_call_id: Option<&str>,
    uid: Option<i64>,
    _gid: Option<i64>,
    _groups: &[i64],
) -> AnfsResult<()> {
    require_path_unlocked_for_mutation(tx, workspace, logical_path, false, now_millis())?;
    require_sticky_entry_mutation_allowed(
        tx,
        &workspace.workspace_name,
        logical_path,
        &old.node_id,
        uid,
    )?;
    validate_state_transition(&old.state, "deleted")?;
    let event_id = new_event_id();
    insert_event(
        tx,
        &event_id,
        "delete_ref",
        Some(&workspace.agent_id),
        workspace.run_id.as_deref(),
        tool_call_id,
        Some(&workspace.workspace_name),
        Some(logical_path),
    )?;
    insert_edge(
        tx,
        &event_id,
        "input",
        &old.node_id,
        "deleted_ref",
        Some(logical_path),
    )?;
    update_ref_state(tx, ref_name, old, "deleted", &event_id)
}

impl WorkspaceRefEntry {
    fn as_ref_record(&self) -> RefRecord {
        RefRecord {
            node_id: self.node_id.clone(),
            ref_kind: "workspace".to_string(),
            state: self.state.clone(),
            ref_version: self.ref_version,
        }
    }
}

fn require_path_readable(workspace: &Workspace, ref_name: &str, rec: &RefRecord) -> AnfsResult<()> {
    let readable = rec.state == "published"
        || rec.state == "approved"
        || workspace.can_read_draft_ref(ref_name, rec);
    if readable {
        Ok(())
    } else {
        Err(AnfsError::PolicyDenied(format!(
            "cannot read ref {ref_name} in state {} from workspace {}",
            rec.state, workspace.workspace_name
        )))
    }
}
