use pyo3::prelude::*;
use pyo3::types::PyBytes;
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde_json::json;
use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use crate::query::query_refs;
use crate::{
    ensure_node_fragments_visible, Inner, now_millis, active_operation_capability_missing, agent_capabilities,
    AgentCapabilityRow, AnfsEngine, AnfsError, AnfsResult, AnswerCostEstimateRow,
    AnswerEvidenceCoverageRow, AnswerQuoteSupportRow, AnswerTokenAccountingRow,
    apply_schema_migrations, ArchivalReadinessRow,
    auto_propagate_fragment_policy_labels_by_exact_match,
    auto_propagate_fragment_policy_labels_by_normalized_json,
    auto_propagate_fragment_policy_labels_by_normalized_scalar, base_child_ref_name,
    cache_materialized_workspace, cache_node_chunks, cached_node_chunks, cached_working_sets,
    CachedWorkingSetResult, CachedWorkingSetRow, checkout_base_refs, ChunkVectorSearchRow,
    collect_garbage, commit_worktree, CompactionPlanRow, create_ref_view_checkpoint,
    DerivedIndexRepairResult, ensure_node_exists, event_list, event_record, EventListRow,
    EventRecord, export_event_bundle, export_history_archive, export_run_bundle,
    ExportBundleResult, fetch_ref, fetch_run, finish_run, fork_workspace_refs,
    fragment_callers, fragment_policy_labels, CallerRow, FragmentPolicyLabelRow, FragmentRow,
    gc_candidates, gc_pins, gc_roots, GcPinRow, index_node_fragments, node_fragments,
    GcResultRow, import_event_bundle, ImportBundleResult, infer_ref_kind, init_db,
    InlineBlobCompactionResult, insert_edge, insert_event, insert_merge_policy_decision_event,
    insert_new_ref, insert_policy_decision_event, json_field_span, json_field_spans,
    JsonFieldSpanRow, lineage_ancestors, lineage_evidence_covers_target, lineage_graph,
    lineage_nodes, LineageGraphRow, lock_conn, manifest_child_records, manifest_children,
    ManifestChildRecord, markdown_field_span, markdown_field_spans, markdown_field_values,
    markdown_section_span, markdown_section_spans, MarkdownFieldSpanRow, MarkdownFieldValueRow,
    MarkdownSectionSpanRow, materialize_blob_file, materialize_ref_view_at_event,
    materialize_workspace, MaterializedViewRow, merge_target_prefix_for_base, new_event_id,
    node_chunk_embedding, node_chunks, node_embedding, node_event_list, NodeChunkRow,
    operation_capability_rules, OperationCapabilityRuleRow, orphan_object_files,
    OrphanObjectCleanupResult, pin_ref, policy_decisions, policy_expression_rules,
    policy_labels, policy_rules, PolicyExpressionRuleRow, PolicyLabelRow, PolicyRuleRow,
    propagate_fragment_policy_labels_between_ranges, purpose_capability_rules,
    purpose_policy_rules, PurposeCapabilityRuleRow, PurposePolicyRuleRow, QueryRefRow,
    read_node_bytes, read_node_range, rebuild_derived_indexes, ref_history, ref_view_at_event,
    ref_view_checkpoints, RefHistoryRow, RefViewCheckpointRow, RefViewCheckpointVerificationRow,
    RefViewRow, reject_unsupported_future_schema, RepairPlanRow, require_expected_ref_version,
    require_ref, require_schema_current, require_state, retention_policies, RetentionPolicyRow,
    run_retention_policy, RunRow, runs, schema_migration_plan, schema_status,
    SchemaMigrationApplyResult, SchemaMigrationPlanRow, SchemaStatusRow, set_agent_capability,
    set_fragment_policy_label, set_node_chunk_embedding, set_node_embedding,
    set_operation_capability_rule, set_policy_expression_rule, set_policy_label,
    set_policy_rule, set_purpose_capability_rule, set_purpose_policy_rule, set_retention_policy,
    sha256_hex, snapshot_namespace, start_run, target_has_producer_agent, TokenCostProfileRow,
    unpin_gc_root, update_ref_node, update_ref_state, VacuumDatabaseResult,
    validate_state_transition, validate_workspace_name, vector_search, vector_search_chunks,
    VectorSearchRow, verify_history_archive_bundle, verify_integrity,
    verify_ref_view_checkpoint, with_sqlite_busy_retry, Workspace,
    workspace_changes_since_checkout, workspace_conflicts, workspace_diff, worktree_readiness,
    WorktreeCommitRow, WorktreeMaterializeRow, WorktreeReadinessRow,
};

#[pymethods]
impl AnfsEngine {
    #[new]
    fn new(db_path: &str, objects_dir: &str) -> PyResult<Self> {
        let objects_dir = PathBuf::from(objects_dir);
        fs::create_dir_all(&objects_dir).map_err(AnfsError::from)?;

        let conn = Connection::open(db_path).map_err(AnfsError::from)?;
        conn.busy_timeout(std::time::Duration::from_millis(30_000))
            .map_err(AnfsError::from)?;
        reject_unsupported_future_schema(&conn).map_err(PyErr::from)?;
        init_db(&conn).map_err(PyErr::from)?;
        require_schema_current(&conn).map_err(PyErr::from)?;

        Ok(Self {
            inner: Arc::new(Inner {
                conn: Mutex::new(conn),
                objects_dir,
            }),
        })
    }

    #[pyo3(signature = (workspace, agent_id, base=None, run_id=None, tool_call_id=None))]
    fn checkout(
        &self,
        workspace: String,
        agent_id: String,
        base: Option<String>,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<Workspace> {
        self.checkout_impl(base, workspace, agent_id, run_id, tool_call_id)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (source_workspace, target_workspace, agent_id, run_id=None, tool_call_id=None))]
    fn fork_workspace(
        &self,
        source_workspace: String,
        target_workspace: String,
        agent_id: String,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<Workspace> {
        self.fork_workspace_impl(
            source_workspace,
            target_workspace,
            agent_id,
            run_id,
            tool_call_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (workspace, agent_id, run_id=None))]
    fn open_workspace(
        &self,
        workspace: String,
        agent_id: String,
        run_id: Option<String>,
    ) -> PyResult<Workspace> {
        let workspace_name = validate_workspace_name(&workspace)?;
        Ok(Workspace {
            inner: self.inner.clone(),
            workspace_name,
            agent_id,
            run_id,
        })
    }

    #[pyo3(signature = (target_ref, evidence_refs, agent_id, run_id=None, tool_call_id=None, expected_version=None))]
    fn approve(
        &self,
        target_ref: &str,
        evidence_refs: Vec<String>,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
        expected_version: Option<i64>,
    ) -> PyResult<()> {
        self.approve_impl(
            target_ref,
            evidence_refs,
            agent_id,
            run_id,
            tool_call_id,
            expected_version,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (target_ref, agent_id, reason=None, run_id=None, tool_call_id=None, expected_version=None))]
    fn reject_ref(
        &self,
        target_ref: &str,
        agent_id: &str,
        reason: Option<String>,
        run_id: Option<String>,
        tool_call_id: Option<String>,
        expected_version: Option<i64>,
    ) -> PyResult<()> {
        self.reject_ref_impl(
            target_ref,
            agent_id,
            reason,
            run_id,
            tool_call_id,
            expected_version,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (ref_name, agent_id, run_id=None, tool_call_id=None, expected_version=None))]
    fn archive_ref(
        &self,
        ref_name: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
        expected_version: Option<i64>,
    ) -> PyResult<()> {
        self.archive_ref_impl(ref_name, agent_id, run_id, tool_call_id, expected_version)
            .map_err(PyErr::from)
    }

    fn get_ref(&self, ref_name: &str) -> PyResult<(String, String, String, String, i64)> {
        let conn = lock_conn(&self.inner)?;
        let rec = require_ref(&conn, ref_name)?;
        Ok((
            ref_name.to_string(),
            rec.node_id,
            rec.ref_kind,
            rec.state,
            rec.ref_version,
        ))
    }

    fn read_node<'py>(&self, py: Python<'py>, node_id: &str) -> PyResult<Bound<'py, PyBytes>> {
        let conn = lock_conn(&self.inner)?;
        ensure_node_fragments_visible(&conn, node_id, || {
            format!("node read blocked by fragment policy label on node {node_id}")
        })
        .map_err(PyErr::from)?;
        let bytes = read_node_bytes(&conn, &self.inner.objects_dir, node_id)?;
        Ok(PyBytes::new_bound(py, &bytes))
    }

    #[pyo3(signature = (node_id, offset, length))]
    fn read_node_range<'py>(
        &self,
        py: Python<'py>,
        node_id: &str,
        offset: i64,
        length: i64,
    ) -> PyResult<Bound<'py, PyBytes>> {
        let conn = lock_conn(&self.inner)?;
        let bytes = read_node_range(&conn, &self.inner.objects_dir, node_id, offset, length)?;
        Ok(PyBytes::new_bound(py, &bytes))
    }

    #[pyo3(signature = (node_id, chunk_size=65536))]
    fn node_chunks(&self, node_id: &str, chunk_size: i64) -> PyResult<Vec<NodeChunkRow>> {
        let conn = lock_conn(&self.inner)?;
        node_chunks(&conn, &self.inner.objects_dir, node_id, chunk_size).map_err(PyErr::from)
    }

    #[pyo3(signature = (node_id, chunk_size=65536))]
    fn cache_node_chunks(&self, node_id: &str, chunk_size: i64) -> PyResult<Vec<NodeChunkRow>> {
        let mut conn = lock_conn(&self.inner)?;
        cache_node_chunks(&mut conn, &self.inner.objects_dir, node_id, chunk_size)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (node_id, chunk_size=65536))]
    fn cached_node_chunks(&self, node_id: &str, chunk_size: i64) -> PyResult<Vec<NodeChunkRow>> {
        let conn = lock_conn(&self.inner)?;
        cached_node_chunks(&conn, node_id, chunk_size).map_err(PyErr::from)
    }

    fn set_node_chunk_embedding(
        &self,
        node_id: &str,
        chunk_size: i64,
        chunk_index: i64,
        model: &str,
        vector: Vec<f64>,
    ) -> PyResult<()> {
        self.set_node_chunk_embedding_impl(node_id, chunk_size, chunk_index, model, vector)
            .map_err(PyErr::from)
    }

    fn node_chunk_embedding(
        &self,
        node_id: &str,
        chunk_size: i64,
        chunk_index: i64,
        model: &str,
    ) -> PyResult<Option<Vec<f64>>> {
        self.node_chunk_embedding_impl(node_id, chunk_size, chunk_index, model)
            .map_err(PyErr::from)
    }

    fn json_field_spans(&self, node_id: &str) -> PyResult<Vec<JsonFieldSpanRow>> {
        let conn = lock_conn(&self.inner)?;
        json_field_spans(&conn, &self.inner.objects_dir, node_id).map_err(PyErr::from)
    }

    fn markdown_field_spans(&self, node_id: &str) -> PyResult<Vec<MarkdownFieldSpanRow>> {
        let conn = lock_conn(&self.inner)?;
        markdown_field_spans(&conn, &self.inner.objects_dir, node_id).map_err(PyErr::from)
    }

    fn markdown_field_values(&self, node_id: &str) -> PyResult<Vec<MarkdownFieldValueRow>> {
        let conn = lock_conn(&self.inner)?;
        markdown_field_values(&conn, &self.inner.objects_dir, node_id).map_err(PyErr::from)
    }

    fn markdown_section_spans(&self, node_id: &str) -> PyResult<Vec<MarkdownSectionSpanRow>> {
        let conn = lock_conn(&self.inner)?;
        markdown_section_spans(&conn, &self.inner.objects_dir, node_id).map_err(PyErr::from)
    }

    /// Parse a node into the generic `fragments` table with the named parser
    /// (`span-markdown` | `span-json`). Idempotent/incremental; returns
    /// `(fragment_count, edge_count)`.
    fn index_node_fragments(&self, node_id: &str, parser: &str) -> PyResult<(i64, i64)> {
        let mut conn = lock_conn(&self.inner)?;
        index_node_fragments(&mut conn, &self.inner.objects_dir, node_id, parser)
            .map_err(PyErr::from)
    }

    /// Outline of a node: its fragments as
    /// `(fragment_id, kind, name, path, byte_start, byte_end)`.
    fn node_fragments(&self, node_id: &str) -> PyResult<Vec<FragmentRow>> {
        let conn = lock_conn(&self.inner)?;
        node_fragments(&conn, node_id).map_err(PyErr::from)
    }

    /// Cross-node callers of `name`: the exact set of call sites across all
    /// indexed nodes, as `(src_fragment_id, src_kind, src_name, src_node_id,
    /// evidence_start, evidence_end)`.
    fn fragment_callers(&self, name: &str) -> PyResult<Vec<CallerRow>> {
        let conn = lock_conn(&self.inner)?;
        fragment_callers(&conn, name).map_err(PyErr::from)
    }

    fn manifest_children(&self, node_id: &str) -> PyResult<Vec<(String, String)>> {
        self.manifest_children_impl(node_id).map_err(PyErr::from)
    }

    fn manifest_child_records(&self, node_id: &str) -> PyResult<Vec<ManifestChildRecord>> {
        self.manifest_child_records_impl(node_id)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (prefix, snapshot_ref, agent_id, kind="resource", run_id=None, tool_call_id=None))]
    fn snapshot_namespace(
        &self,
        prefix: &str,
        snapshot_ref: &str,
        agent_id: &str,
        kind: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<String> {
        self.snapshot_namespace_impl(
            prefix,
            snapshot_ref,
            agent_id,
            kind,
            run_id.as_deref(),
            tool_call_id.as_deref(),
        )
        .map_err(PyErr::from)
    }

    fn verify_integrity(&self) -> PyResult<Vec<String>> {
        self.verify_integrity_impl().map_err(PyErr::from)
    }

    fn rebuild_derived_indexes(&self) -> PyResult<DerivedIndexRepairResult> {
        self.rebuild_derived_indexes_impl().map_err(PyErr::from)
    }

    fn repair_plan(&self) -> PyResult<Vec<RepairPlanRow>> {
        self.repair_plan_impl().map_err(PyErr::from)
    }

    fn compaction_plan(&self) -> PyResult<Vec<CompactionPlanRow>> {
        self.compaction_plan_impl().map_err(PyErr::from)
    }

    #[pyo3(signature = (checkpoint_id, bundle_path, signature_key=None, require_signature=false))]
    fn archival_readiness_plan(
        &self,
        checkpoint_id: &str,
        bundle_path: &str,
        signature_key: Option<String>,
        require_signature: bool,
    ) -> PyResult<Vec<ArchivalReadinessRow>> {
        self.archival_readiness_plan_impl(
            checkpoint_id,
            bundle_path,
            signature_key,
            require_signature,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (dry_run=true))]
    fn vacuum_database(&self, dry_run: bool) -> PyResult<VacuumDatabaseResult> {
        self.vacuum_database_impl(dry_run).map_err(PyErr::from)
    }

    #[pyo3(signature = (dry_run=true, limit=None))]
    fn compact_inline_blobs(
        &self,
        dry_run: bool,
        limit: Option<i64>,
    ) -> PyResult<InlineBlobCompactionResult> {
        self.compact_inline_blobs_impl(dry_run, limit)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (dry_run=true, limit=None))]
    fn clean_orphan_objects(
        &self,
        dry_run: bool,
        limit: Option<i64>,
    ) -> PyResult<OrphanObjectCleanupResult> {
        self.clean_orphan_objects_impl(dry_run, limit)
            .map_err(PyErr::from)
    }

    fn schema_status(&self) -> PyResult<Vec<SchemaStatusRow>> {
        self.schema_status_impl().map_err(PyErr::from)
    }

    fn schema_migration_plan(&self) -> PyResult<Vec<SchemaMigrationPlanRow>> {
        self.schema_migration_plan_impl().map_err(PyErr::from)
    }

    #[pyo3(signature = (dry_run=true))]
    fn apply_schema_migrations(&self, dry_run: bool) -> PyResult<SchemaMigrationApplyResult> {
        self.apply_schema_migrations_impl(dry_run)
            .map_err(PyErr::from)
    }

    fn require_schema_current(&self) -> PyResult<()> {
        self.require_schema_current_impl().map_err(PyErr::from)
    }

    fn set_node_embedding(&self, node_id: &str, model: &str, vector: Vec<f64>) -> PyResult<()> {
        self.set_node_embedding_impl(node_id, model, vector)
            .map_err(PyErr::from)
    }

    fn node_embedding(&self, node_id: &str, model: &str) -> PyResult<Option<Vec<f64>>> {
        self.node_embedding_impl(node_id, model)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (model, query_vector, state="published", limit=10, policy_label_excludes=None))]
    fn vector_search(
        &self,
        model: &str,
        query_vector: Vec<f64>,
        state: &str,
        limit: i64,
        policy_label_excludes: Option<Vec<String>>,
    ) -> PyResult<Vec<VectorSearchRow>> {
        self.vector_search_impl(
            model,
            query_vector,
            state,
            limit,
            policy_label_excludes.unwrap_or_default(),
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (model, query_vector, state="published", limit=10, policy_label_excludes=None))]
    fn vector_search_chunks(
        &self,
        model: &str,
        query_vector: Vec<f64>,
        state: &str,
        limit: i64,
        policy_label_excludes: Option<Vec<String>>,
    ) -> PyResult<Vec<ChunkVectorSearchRow>> {
        self.vector_search_chunks_impl(
            model,
            query_vector,
            state,
            limit,
            policy_label_excludes.unwrap_or_default(),
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (run_id, agent_id=None, workspace_id=None, metadata_json=None))]
    fn start_run(
        &self,
        run_id: &str,
        agent_id: Option<String>,
        workspace_id: Option<String>,
        metadata_json: Option<String>,
    ) -> PyResult<RunRow> {
        self.start_run_impl(run_id, agent_id, workspace_id, metadata_json)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (run_id, state="succeeded", metadata_json=None))]
    fn finish_run(
        &self,
        run_id: &str,
        state: &str,
        metadata_json: Option<String>,
    ) -> PyResult<RunRow> {
        self.finish_run_impl(run_id, state, metadata_json)
            .map_err(PyErr::from)
    }

    fn get_run(&self, run_id: &str) -> PyResult<RunRow> {
        self.get_run_impl(run_id).map_err(PyErr::from)
    }

    #[pyo3(signature = (state=None, agent_id=None, workspace_id=None))]
    fn runs(
        &self,
        state: Option<String>,
        agent_id: Option<String>,
        workspace_id: Option<String>,
    ) -> PyResult<Vec<RunRow>> {
        self.runs_impl(state, agent_id, workspace_id)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (target_ref=None, policy=None, decision=None, reason_code=None))]
    fn policy_decisions(
        &self,
        target_ref: Option<String>,
        policy: Option<String>,
        decision: Option<String>,
        reason_code: Option<String>,
    ) -> PyResult<Vec<(String, String)>> {
        self.policy_decisions_impl(target_ref, policy, decision, reason_code)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (subject_type, subject_id, label, value, agent_id, run_id=None, tool_call_id=None))]
    fn set_policy_label(
        &self,
        subject_type: &str,
        subject_id: &str,
        label: &str,
        value: Option<String>,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<()> {
        self.set_policy_label_impl(
            subject_type,
            subject_id,
            label,
            value,
            agent_id,
            run_id,
            tool_call_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (subject_type=None, subject_id=None, label=None, active_only=true))]
    fn policy_labels(
        &self,
        subject_type: Option<String>,
        subject_id: Option<String>,
        label: Option<String>,
        active_only: bool,
    ) -> PyResult<Vec<PolicyLabelRow>> {
        self.policy_labels_impl(subject_type, subject_id, label, active_only)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (node_id, offset, length, label, value, agent_id, run_id=None, tool_call_id=None))]
    fn set_fragment_policy_label(
        &self,
        node_id: &str,
        offset: i64,
        length: i64,
        label: &str,
        value: Option<String>,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<()> {
        self.set_fragment_policy_label_impl(
            node_id,
            offset,
            length,
            label,
            value,
            agent_id,
            run_id,
            tool_call_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (node_id=None, label=None, active_only=true))]
    fn fragment_policy_labels(
        &self,
        node_id: Option<String>,
        label: Option<String>,
        active_only: bool,
    ) -> PyResult<Vec<FragmentPolicyLabelRow>> {
        self.fragment_policy_labels_impl(node_id, label, active_only)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (source_node_id, source_offset, source_length, output_node_id, output_offset, output_length, agent_id, run_id=None, tool_call_id=None))]
    fn propagate_fragment_policy_labels(
        &self,
        source_node_id: &str,
        source_offset: i64,
        source_length: i64,
        output_node_id: &str,
        output_offset: i64,
        output_length: i64,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<i64> {
        self.propagate_fragment_policy_labels_impl(
            source_node_id,
            source_offset,
            source_length,
            output_node_id,
            output_offset,
            output_length,
            agent_id,
            run_id,
            tool_call_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (source_node_id, output_node_id, agent_id, run_id=None, tool_call_id=None))]
    fn auto_propagate_fragment_policy_labels(
        &self,
        source_node_id: &str,
        output_node_id: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<i64> {
        self.auto_propagate_fragment_policy_labels_impl(
            source_node_id,
            output_node_id,
            agent_id,
            run_id,
            tool_call_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (source_node_id, output_node_id, agent_id, run_id=None, tool_call_id=None))]
    fn auto_propagate_fragment_policy_labels_by_normalized_scalar(
        &self,
        source_node_id: &str,
        output_node_id: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<i64> {
        self.auto_propagate_fragment_policy_labels_by_normalized_scalar_impl(
            source_node_id,
            output_node_id,
            agent_id,
            run_id,
            tool_call_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (source_node_id, output_node_id, agent_id, run_id=None, tool_call_id=None))]
    fn auto_propagate_fragment_policy_labels_by_normalized_json(
        &self,
        source_node_id: &str,
        output_node_id: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<i64> {
        self.auto_propagate_fragment_policy_labels_by_normalized_json_impl(
            source_node_id,
            output_node_id,
            agent_id,
            run_id,
            tool_call_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (node_id, json_path, label, value, agent_id, run_id=None, tool_call_id=None))]
    fn set_json_field_policy_label(
        &self,
        node_id: &str,
        json_path: &str,
        label: &str,
        value: Option<String>,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<()> {
        self.set_json_field_policy_label_impl(
            node_id,
            json_path,
            label,
            value,
            agent_id,
            run_id,
            tool_call_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (node_id, field_path, label, value, agent_id, run_id=None, tool_call_id=None))]
    fn set_markdown_field_policy_label(
        &self,
        node_id: &str,
        field_path: &str,
        label: &str,
        value: Option<String>,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<()> {
        self.set_markdown_field_policy_label_impl(
            node_id,
            field_path,
            label,
            value,
            agent_id,
            run_id,
            tool_call_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (node_id, section_path, label, value, agent_id, run_id=None, tool_call_id=None))]
    fn set_markdown_section_policy_label(
        &self,
        node_id: &str,
        section_path: &str,
        label: &str,
        value: Option<String>,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<()> {
        self.set_markdown_section_policy_label_impl(
            node_id,
            section_path,
            label,
            value,
            agent_id,
            run_id,
            tool_call_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (label, value=None, effect="deny", scope="visibility", subject_type="*", agent_id="policy_agent", run_id=None, tool_call_id=None))]
    fn set_policy_rule(
        &self,
        label: &str,
        value: Option<String>,
        effect: &str,
        scope: &str,
        subject_type: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<()> {
        self.set_policy_rule_impl(
            label,
            value,
            Some(effect.to_string()),
            scope,
            subject_type,
            agent_id,
            run_id,
            tool_call_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (label, value=None, scope="visibility", subject_type="*", agent_id="policy_agent", run_id=None, tool_call_id=None))]
    fn clear_policy_rule(
        &self,
        label: &str,
        value: Option<String>,
        scope: &str,
        subject_type: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<()> {
        self.set_policy_rule_impl(
            label,
            value,
            None,
            scope,
            subject_type,
            agent_id,
            run_id,
            tool_call_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (scope=None, subject_type=None, label=None, active_only=true))]
    fn policy_rules(
        &self,
        scope: Option<String>,
        subject_type: Option<String>,
        label: Option<String>,
        active_only: bool,
    ) -> PyResult<Vec<PolicyRuleRow>> {
        self.policy_rules_impl(scope, subject_type, label, active_only)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (purpose, label, value=None, effect="deny", subject_type="*", agent_id="policy_agent", run_id=None, tool_call_id=None))]
    fn set_purpose_policy_rule(
        &self,
        purpose: &str,
        label: &str,
        value: Option<String>,
        effect: &str,
        subject_type: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<()> {
        self.set_purpose_policy_rule_impl(
            purpose,
            label,
            value,
            Some(effect.to_string()),
            subject_type,
            agent_id,
            run_id,
            tool_call_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (purpose, label, value=None, subject_type="*", agent_id="policy_agent", run_id=None, tool_call_id=None))]
    fn clear_purpose_policy_rule(
        &self,
        purpose: &str,
        label: &str,
        value: Option<String>,
        subject_type: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<()> {
        self.set_purpose_policy_rule_impl(
            purpose,
            label,
            value,
            None,
            subject_type,
            agent_id,
            run_id,
            tool_call_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (purpose=None, subject_type=None, label=None, active_only=true))]
    fn purpose_policy_rules(
        &self,
        purpose: Option<String>,
        subject_type: Option<String>,
        label: Option<String>,
        active_only: bool,
    ) -> PyResult<Vec<PurposePolicyRuleRow>> {
        self.purpose_policy_rules_impl(purpose, subject_type, label, active_only)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (target_agent_id, capability, agent_id="policy_agent", run_id=None, tool_call_id=None))]
    fn grant_agent_capability(
        &self,
        target_agent_id: &str,
        capability: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<()> {
        self.set_agent_capability_impl(
            target_agent_id,
            capability,
            Some("grant".to_string()),
            agent_id,
            run_id,
            tool_call_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (target_agent_id, capability, agent_id="policy_agent", run_id=None, tool_call_id=None))]
    fn revoke_agent_capability(
        &self,
        target_agent_id: &str,
        capability: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<()> {
        self.set_agent_capability_impl(
            target_agent_id,
            capability,
            None,
            agent_id,
            run_id,
            tool_call_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (agent_id=None, capability=None, active_only=true))]
    fn agent_capabilities(
        &self,
        agent_id: Option<String>,
        capability: Option<String>,
        active_only: bool,
    ) -> PyResult<Vec<AgentCapabilityRow>> {
        self.agent_capabilities_impl(agent_id, capability, active_only)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (purpose, capability, effect="require", agent_id="policy_agent", run_id=None, tool_call_id=None))]
    fn set_purpose_capability_rule(
        &self,
        purpose: &str,
        capability: &str,
        effect: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<()> {
        self.set_purpose_capability_rule_impl(
            purpose,
            capability,
            Some(effect.to_string()),
            agent_id,
            run_id,
            tool_call_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (purpose, capability, agent_id="policy_agent", run_id=None, tool_call_id=None))]
    fn clear_purpose_capability_rule(
        &self,
        purpose: &str,
        capability: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<()> {
        self.set_purpose_capability_rule_impl(
            purpose,
            capability,
            None,
            agent_id,
            run_id,
            tool_call_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (purpose=None, capability=None, active_only=true))]
    fn purpose_capability_rules(
        &self,
        purpose: Option<String>,
        capability: Option<String>,
        active_only: bool,
    ) -> PyResult<Vec<PurposeCapabilityRuleRow>> {
        self.purpose_capability_rules_impl(purpose, capability, active_only)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (operation, capability, effect="require", agent_id="policy_agent", run_id=None, tool_call_id=None))]
    fn set_operation_capability_rule(
        &self,
        operation: &str,
        capability: &str,
        effect: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<()> {
        self.set_operation_capability_rule_impl(
            operation,
            capability,
            Some(effect.to_string()),
            agent_id,
            run_id,
            tool_call_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (operation, capability, agent_id="policy_agent", run_id=None, tool_call_id=None))]
    fn clear_operation_capability_rule(
        &self,
        operation: &str,
        capability: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<()> {
        self.set_operation_capability_rule_impl(
            operation,
            capability,
            None,
            agent_id,
            run_id,
            tool_call_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (operation=None, capability=None, active_only=true))]
    fn operation_capability_rules(
        &self,
        operation: Option<String>,
        capability: Option<String>,
        active_only: bool,
    ) -> PyResult<Vec<OperationCapabilityRuleRow>> {
        self.operation_capability_rules_impl(operation, capability, active_only)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (expression_json, effect="deny", scope="visibility", subject_type="*", agent_id="policy_agent", run_id=None, tool_call_id=None))]
    fn set_policy_expression_rule(
        &self,
        expression_json: &str,
        effect: &str,
        scope: &str,
        subject_type: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<()> {
        self.set_policy_expression_rule_impl(
            expression_json,
            Some(effect.to_string()),
            scope,
            subject_type,
            agent_id,
            run_id,
            tool_call_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (expression_json, scope="visibility", subject_type="*", agent_id="policy_agent", run_id=None, tool_call_id=None))]
    fn clear_policy_expression_rule(
        &self,
        expression_json: &str,
        scope: &str,
        subject_type: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<()> {
        self.set_policy_expression_rule_impl(
            expression_json,
            None,
            scope,
            subject_type,
            agent_id,
            run_id,
            tool_call_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (scope=None, subject_type=None, active_only=true))]
    fn policy_expression_rules(
        &self,
        scope: Option<String>,
        subject_type: Option<String>,
        active_only: bool,
    ) -> PyResult<Vec<PolicyExpressionRuleRow>> {
        self.policy_expression_rules_impl(scope, subject_type, active_only)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (prefix=None, text=None, state=None, agent_id=None, run_id=None, event_kind=None, media_type=None, created_after_ms=None, created_before_ms=None, policy=None, decision=None, reason_code=None, limit=100, policy_label_excludes=None, purpose=None))]
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
            policy_label_excludes.unwrap_or_default(),
            purpose,
        )
        .map_err(PyErr::from)
    }

    fn ref_history(&self, ref_name: &str) -> PyResult<Vec<RefHistoryRow>> {
        self.ref_history_impl(ref_name).map_err(PyErr::from)
    }

    fn event(&self, event_id: &str) -> PyResult<EventRecord> {
        self.event_impl(event_id).map_err(PyErr::from)
    }

    fn answer_evidence_coverage(
        &self,
        answer_event_id: &str,
    ) -> PyResult<Vec<AnswerEvidenceCoverageRow>> {
        self.answer_evidence_coverage_impl(answer_event_id)
            .map_err(PyErr::from)
    }

    fn answer_token_accounting(&self, answer_event_id: &str) -> PyResult<AnswerTokenAccountingRow> {
        self.answer_token_accounting_impl(answer_event_id)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (
        model,
        input_price_micros_per_million_tokens,
        output_price_micros_per_million_tokens,
        prompt_overhead_tokens=0,
        agent_id="cost_profile_agent",
        run_id=None,
        tool_call_id=None
    ))]
    fn set_token_cost_profile(
        &self,
        model: &str,
        input_price_micros_per_million_tokens: i64,
        output_price_micros_per_million_tokens: i64,
        prompt_overhead_tokens: i64,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<String> {
        self.set_token_cost_profile_impl(
            model,
            input_price_micros_per_million_tokens,
            output_price_micros_per_million_tokens,
            prompt_overhead_tokens,
            agent_id,
            run_id,
            tool_call_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (active_only=true))]
    fn token_cost_profiles(&self, active_only: bool) -> PyResult<Vec<TokenCostProfileRow>> {
        self.token_cost_profiles_impl(active_only)
            .map_err(PyErr::from)
    }

    fn answer_cost_estimate(
        &self,
        answer_event_id: &str,
        model: &str,
    ) -> PyResult<AnswerCostEstimateRow> {
        self.answer_cost_estimate_impl(answer_event_id, model)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (answer_event_id, min_quote_bytes=16))]
    fn answer_quote_support(
        &self,
        answer_event_id: &str,
        min_quote_bytes: i64,
    ) -> PyResult<Vec<AnswerQuoteSupportRow>> {
        self.answer_quote_support_impl(answer_event_id, min_quote_bytes)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (after_seq=0, limit=100, kind=None, agent_id=None, workspace_id=None, run_id=None, created_after_ms=None, created_before_ms=None, payload_contains=None, tool_call_id=None))]
    fn events(
        &self,
        after_seq: i64,
        limit: i64,
        kind: Option<String>,
        agent_id: Option<String>,
        workspace_id: Option<String>,
        run_id: Option<String>,
        created_after_ms: Option<i64>,
        created_before_ms: Option<i64>,
        payload_contains: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<Vec<EventListRow>> {
        self.events_impl(
            after_seq,
            limit,
            kind,
            agent_id,
            workspace_id,
            run_id,
            created_after_ms,
            created_before_ms,
            payload_contains,
            tool_call_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (node_id, direction=None, role=None, after_seq=0, limit=100))]
    fn node_events(
        &self,
        node_id: &str,
        direction: Option<String>,
        role: Option<String>,
        after_seq: i64,
        limit: i64,
    ) -> PyResult<Vec<EventListRow>> {
        self.node_events_impl(node_id, direction, role, after_seq, limit)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (node_id, direction="ancestors"))]
    fn lineage_nodes(&self, node_id: &str, direction: &str) -> PyResult<Vec<String>> {
        self.lineage_nodes_impl(node_id, direction)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (node_id, direction="ancestors"))]
    fn lineage_graph(&self, node_id: &str, direction: &str) -> PyResult<Vec<LineageGraphRow>> {
        self.lineage_graph_impl(node_id, direction)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (event_id, prefix=None, include_inactive=false, policy_label_excludes=None))]
    fn ref_view_at_event(
        &self,
        event_id: &str,
        prefix: Option<String>,
        include_inactive: bool,
        policy_label_excludes: Option<Vec<String>>,
    ) -> PyResult<Vec<RefViewRow>> {
        self.ref_view_at_event_impl(
            event_id,
            prefix,
            include_inactive,
            policy_label_excludes.unwrap_or_default(),
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (event_id=None, prefix=None, include_inactive=false, agent_id="checkpoint_agent"))]
    fn create_ref_view_checkpoint(
        &self,
        event_id: Option<String>,
        prefix: Option<String>,
        include_inactive: bool,
        agent_id: &str,
    ) -> PyResult<RefViewCheckpointRow> {
        self.create_ref_view_checkpoint_impl(event_id, prefix, include_inactive, agent_id)
            .map_err(PyErr::from)
    }

    fn ref_view_checkpoints(&self) -> PyResult<Vec<RefViewCheckpointRow>> {
        self.ref_view_checkpoints_impl().map_err(PyErr::from)
    }

    fn verify_ref_view_checkpoint(
        &self,
        checkpoint_id: &str,
    ) -> PyResult<RefViewCheckpointVerificationRow> {
        self.verify_ref_view_checkpoint_impl(checkpoint_id)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (event_id, output_dir, prefix=None, include_inactive=false, overwrite=false, atomic=true, policy_label_excludes=None))]
    fn materialize_ref_view_at_event(
        &self,
        event_id: &str,
        output_dir: &str,
        prefix: Option<String>,
        include_inactive: bool,
        overwrite: bool,
        atomic: bool,
        policy_label_excludes: Option<Vec<String>>,
    ) -> PyResult<Vec<MaterializedViewRow>> {
        self.materialize_ref_view_at_event_impl(
            event_id,
            output_dir,
            prefix,
            include_inactive,
            overwrite,
            atomic,
            policy_label_excludes.unwrap_or_default(),
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (workspace, output_dir, overwrite=false, atomic=true, policy_label_excludes=None))]
    fn materialize_workspace(
        &self,
        workspace: &str,
        output_dir: &str,
        overwrite: bool,
        atomic: bool,
        policy_label_excludes: Option<Vec<String>>,
    ) -> PyResult<Vec<WorktreeMaterializeRow>> {
        self.materialize_workspace_impl(
            workspace,
            output_dir,
            overwrite,
            atomic,
            policy_label_excludes.unwrap_or_default(),
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (workspace, cache_root, cache_key=None, overwrite=false, atomic=true, policy_label_excludes=None))]
    fn cache_materialized_workspace(
        &self,
        workspace: &str,
        cache_root: &str,
        cache_key: Option<String>,
        overwrite: bool,
        atomic: bool,
        policy_label_excludes: Option<Vec<String>>,
    ) -> PyResult<CachedWorkingSetResult> {
        self.cache_materialized_workspace_impl(
            workspace,
            cache_root,
            cache_key,
            overwrite,
            atomic,
            policy_label_excludes.unwrap_or_default(),
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (workspace=None))]
    fn cached_working_sets(&self, workspace: Option<String>) -> PyResult<Vec<CachedWorkingSetRow>> {
        let conn = lock_conn(&self.inner)?;
        cached_working_sets(&conn, workspace.as_deref()).map_err(PyErr::from)
    }

    #[pyo3(signature = (workspace, input_dir, agent_id, run_id=None, tool_call_id=None, delete_missing=true, require_manifest_match=false, policy_label_excludes=None))]
    fn commit_worktree(
        &self,
        workspace: &str,
        input_dir: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
        delete_missing: bool,
        require_manifest_match: bool,
        policy_label_excludes: Option<Vec<String>>,
    ) -> PyResult<Vec<WorktreeCommitRow>> {
        self.commit_worktree_impl(
            workspace,
            input_dir,
            agent_id,
            run_id,
            tool_call_id,
            delete_missing,
            require_manifest_match,
            policy_label_excludes.unwrap_or_default(),
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (workspace, input_dir, policy_label_excludes=None))]
    fn worktree_readiness(
        &self,
        workspace: &str,
        input_dir: &str,
        policy_label_excludes: Option<Vec<String>>,
    ) -> PyResult<Vec<WorktreeReadinessRow>> {
        self.worktree_readiness_impl(
            workspace,
            input_dir,
            policy_label_excludes.unwrap_or_default(),
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (event_id, output_dir, include_blobs=true, policy_label_excludes=None, signing_key=None, signer_id=None))]
    fn export_event_bundle(
        &self,
        event_id: &str,
        output_dir: &str,
        include_blobs: bool,
        policy_label_excludes: Option<Vec<String>>,
        signing_key: Option<String>,
        signer_id: Option<String>,
    ) -> PyResult<ExportBundleResult> {
        self.export_event_bundle_impl(
            event_id,
            output_dir,
            include_blobs,
            policy_label_excludes.unwrap_or_default(),
            signing_key,
            signer_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (run_id, output_dir, include_blobs=true, policy_label_excludes=None, signing_key=None, signer_id=None))]
    fn export_run_bundle(
        &self,
        run_id: &str,
        output_dir: &str,
        include_blobs: bool,
        policy_label_excludes: Option<Vec<String>>,
        signing_key: Option<String>,
        signer_id: Option<String>,
    ) -> PyResult<ExportBundleResult> {
        self.export_run_bundle_impl(
            run_id,
            output_dir,
            include_blobs,
            policy_label_excludes.unwrap_or_default(),
            signing_key,
            signer_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (output_dir, include_blobs=true, policy_label_excludes=None, signing_key=None, signer_id=None))]
    fn export_history_archive(
        &self,
        output_dir: &str,
        include_blobs: bool,
        policy_label_excludes: Option<Vec<String>>,
        signing_key: Option<String>,
        signer_id: Option<String>,
    ) -> PyResult<ExportBundleResult> {
        self.export_history_archive_impl(
            output_dir,
            include_blobs,
            policy_label_excludes.unwrap_or_default(),
            signing_key,
            signer_id,
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (bundle_path, bundle_objects_dir=None, policy_label_excludes=None, signature_key=None, require_signature=false))]
    fn import_event_bundle(
        &self,
        bundle_path: &str,
        bundle_objects_dir: Option<String>,
        policy_label_excludes: Option<Vec<String>>,
        signature_key: Option<String>,
        require_signature: bool,
    ) -> PyResult<ImportBundleResult> {
        self.import_event_bundle_impl(
            bundle_path,
            bundle_objects_dir,
            policy_label_excludes.unwrap_or_default(),
            signature_key,
            require_signature,
        )
        .map_err(PyErr::from)
    }

    fn workspace_diff(
        &self,
        base: &str,
        workspace: &str,
    ) -> PyResult<Vec<(String, String, Option<String>, Option<String>)>> {
        self.workspace_diff_impl(base, workspace)
            .map_err(PyErr::from)
    }

    fn workspace_conflicts(
        &self,
        base: &str,
        workspace: &str,
    ) -> PyResult<Vec<(String, Option<String>, Option<String>, Option<String>)>> {
        self.workspace_conflicts_impl(base, workspace)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (base, workspace, agent_id, run_id=None, tool_call_id=None))]
    fn merge_workspace(
        &self,
        base: &str,
        workspace: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> PyResult<Vec<(String, String, String, Option<String>)>> {
        self.merge_workspace_impl(base, workspace, agent_id, run_id, tool_call_id)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (include_workspaces=true))]
    fn gc_roots(&self, include_workspaces: bool) -> PyResult<Vec<(String, String, String)>> {
        self.gc_roots_impl(include_workspaces).map_err(PyErr::from)
    }

    #[pyo3(signature = (include_workspaces=true, older_than_ms=None, limit=None))]
    fn gc_candidates(
        &self,
        include_workspaces: bool,
        older_than_ms: Option<i64>,
        limit: Option<i64>,
    ) -> PyResult<Vec<(String, i64, String)>> {
        self.gc_candidates_impl(include_workspaces, older_than_ms, limit)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (include_workspaces=true, dry_run=true, agent_id="gc", older_than_ms=None, limit=None))]
    fn collect_garbage(
        &self,
        include_workspaces: bool,
        dry_run: bool,
        agent_id: &str,
        older_than_ms: Option<i64>,
        limit: Option<i64>,
    ) -> PyResult<Vec<GcResultRow>> {
        self.collect_garbage_impl(include_workspaces, dry_run, agent_id, older_than_ms, limit)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (ref_name, reason=None, agent_id="gc", run_id=None))]
    fn pin_ref(
        &self,
        ref_name: &str,
        reason: Option<String>,
        agent_id: &str,
        run_id: Option<String>,
    ) -> PyResult<String> {
        self.pin_ref_impl(ref_name, reason.as_deref(), agent_id, run_id.as_deref())
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (pin_id, agent_id="gc", run_id=None))]
    fn unpin_gc_root(&self, pin_id: &str, agent_id: &str, run_id: Option<String>) -> PyResult<()> {
        self.unpin_gc_root_impl(pin_id, agent_id, run_id.as_deref())
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (active_only=true))]
    fn gc_pins(&self, active_only: bool) -> PyResult<Vec<GcPinRow>> {
        self.gc_pins_impl(active_only).map_err(PyErr::from)
    }

    #[pyo3(signature = (policy_name, include_workspaces=true, min_age_ms=None, limit=None, enabled=true, agent_id="retention_agent", run_id=None))]
    fn set_retention_policy(
        &self,
        policy_name: &str,
        include_workspaces: bool,
        min_age_ms: Option<i64>,
        limit: Option<i64>,
        enabled: bool,
        agent_id: &str,
        run_id: Option<String>,
    ) -> PyResult<()> {
        self.set_retention_policy_impl(
            policy_name,
            include_workspaces,
            min_age_ms,
            limit,
            enabled,
            agent_id,
            run_id.as_deref(),
        )
        .map_err(PyErr::from)
    }

    #[pyo3(signature = (active_only=true))]
    fn retention_policies(&self, active_only: bool) -> PyResult<Vec<RetentionPolicyRow>> {
        self.retention_policies_impl(active_only)
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (policy_name, dry_run=true, agent_id="retention_agent"))]
    fn run_retention_policy(
        &self,
        policy_name: &str,
        dry_run: bool,
        agent_id: &str,
    ) -> PyResult<Vec<GcResultRow>> {
        self.run_retention_policy_impl(policy_name, dry_run, agent_id)
            .map_err(PyErr::from)
    }
}

impl AnfsEngine {
    fn checkout_impl(
        &self,
        base: Option<String>,
        workspace: String,
        agent_id: String,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<Workspace> {
        let workspace = validate_workspace_name(&workspace)?;
        let mut conn = lock_conn(&self.inner)?;
        let tx = conn.transaction()?;
        let event_id = new_event_id();
        insert_event(
            &tx,
            &event_id,
            "checkout",
            Some(&agent_id),
            run_id.as_deref(),
            tool_call_id.as_deref(),
            Some(&workspace),
            base.as_deref(),
        )?;
        if let Some(base) = base.as_deref() {
            checkout_base_refs(&tx, &self.inner.objects_dir, base, &workspace, &event_id)?;
        }
        tx.commit()?;

        Ok(Workspace {
            inner: self.inner.clone(),
            workspace_name: workspace,
            agent_id,
            run_id,
        })
    }

    fn fork_workspace_impl(
        &self,
        source_workspace: String,
        target_workspace: String,
        agent_id: String,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<Workspace> {
        let source_workspace = validate_workspace_name(&source_workspace)?;
        let target_workspace = validate_workspace_name(&target_workspace)?;
        let mut conn = lock_conn(&self.inner)?;
        let tx = conn.transaction()?;
        let event_id = new_event_id();
        let payload = json!({
            "source_workspace": source_workspace,
            "target_workspace": target_workspace,
        })
        .to_string();
        insert_event(
            &tx,
            &event_id,
            "fork_workspace",
            Some(&agent_id),
            run_id.as_deref(),
            tool_call_id.as_deref(),
            Some(&target_workspace),
            Some(&payload),
        )?;
        fork_workspace_refs(&tx, &source_workspace, &target_workspace, &event_id)?;
        tx.commit()?;

        Ok(Workspace {
            inner: self.inner.clone(),
            workspace_name: target_workspace,
            agent_id,
            run_id,
        })
    }

    fn approve_impl(
        &self,
        target_ref: &str,
        evidence_refs: Vec<String>,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
        expected_version: Option<i64>,
    ) -> AnfsResult<()> {
        if evidence_refs.is_empty() {
            return Err(AnfsError::PolicyDenied(
                "approval requires at least one evidence ref".to_string(),
            ));
        }

        let mut conn = lock_conn(&self.inner)?;
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        require_operation_capability(&tx, "approve", agent_id)?;
        let target = require_ref(&tx, target_ref)?;
        require_expected_ref_version(target_ref, &target, expected_version)?;
        require_state(&target.state, "published", "approve")?;
        if target_has_producer_agent(&tx, &target.node_id, agent_id)? {
            let reason = format!(
                "agent {agent_id} cannot approve ref {target_ref} because it produced the target node"
            );
            insert_policy_decision_event(
                &tx,
                "review_separation",
                "deny",
                "self_review_denied",
                &reason,
                Some(agent_id),
                Some(target_ref),
                &target.node_id,
                &[],
                run_id.as_deref(),
                tool_call_id.as_deref(),
            )?;
            tx.commit()?;
            return Err(AnfsError::PolicyDenied(reason));
        }

        let mut evidence_nodes = Vec::with_capacity(evidence_refs.len());
        let mut evidence_ancestors = HashSet::new();
        for evidence_ref in evidence_refs {
            let evidence = require_ref(&tx, &evidence_ref)?;
            for ancestor in lineage_ancestors(&tx, &evidence.node_id)? {
                evidence_ancestors.insert(ancestor);
            }
            evidence_nodes.push((evidence_ref, evidence.node_id));
        }
        let covered = lineage_evidence_covers_target(
            &tx,
            &self.inner.objects_dir,
            &target.node_id,
            &evidence_ancestors,
        )?;
        if !covered {
            let reason = format!(
                "evidence refs do not derive from target ref {target_ref} ({})",
                target.node_id
            );
            insert_policy_decision_event(
                &tx,
                "lineage_approval",
                "deny",
                "lineage_evidence_missing",
                &reason,
                Some(agent_id),
                Some(target_ref),
                &target.node_id,
                &evidence_nodes,
                run_id.as_deref(),
                tool_call_id.as_deref(),
            )?;
            tx.commit()?;
            return Err(AnfsError::LineageMismatch(reason.to_string()));
        }

        insert_policy_decision_event(
            &tx,
            "lineage_approval",
            "allow",
            "lineage_evidence_covers_target",
            "evidence covers target lineage",
            Some(agent_id),
            Some(target_ref),
            &target.node_id,
            &evidence_nodes,
            run_id.as_deref(),
            tool_call_id.as_deref(),
        )?;

        let event_id = new_event_id();
        insert_event(
            &tx,
            &event_id,
            "approve",
            Some(agent_id),
            run_id.as_deref(),
            tool_call_id.as_deref(),
            None,
            Some(target_ref),
        )?;
        insert_edge(&tx, &event_id, "input", &target.node_id, "target", None)?;
        for (evidence_ref, evidence_node) in &evidence_nodes {
            insert_edge(
                &tx,
                &event_id,
                "input",
                evidence_node,
                "evidence",
                Some(evidence_ref),
            )?;
        }
        insert_edge(
            &tx,
            &event_id,
            "output",
            &target.node_id,
            "approved_target",
            Some(target_ref),
        )?;

        update_ref_state(&tx, target_ref, &target, "approved", &event_id)?;
        tx.commit()?;
        Ok(())
    }

    fn reject_ref_impl(
        &self,
        target_ref: &str,
        agent_id: &str,
        reason: Option<String>,
        run_id: Option<String>,
        tool_call_id: Option<String>,
        expected_version: Option<i64>,
    ) -> AnfsResult<()> {
        let mut conn = lock_conn(&self.inner)?;
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        require_operation_capability(&tx, "reject_ref", agent_id)?;
        let target = require_ref(&tx, target_ref)?;
        require_expected_ref_version(target_ref, &target, expected_version)?;
        validate_state_transition(&target.state, "rejected")?;
        if target_has_producer_agent(&tx, &target.node_id, agent_id)? {
            let reason = format!(
                "agent {agent_id} cannot reject ref {target_ref} because it produced the target node"
            );
            insert_policy_decision_event(
                &tx,
                "review_separation",
                "deny",
                "self_review_denied",
                &reason,
                Some(agent_id),
                Some(target_ref),
                &target.node_id,
                &[],
                run_id.as_deref(),
                tool_call_id.as_deref(),
            )?;
            tx.commit()?;
            return Err(AnfsError::PolicyDenied(reason));
        }

        let event_id = new_event_id();
        let payload = reason
            .as_ref()
            .map(|reason| json!({ "target_ref": target_ref, "reason": reason }).to_string());
        insert_event(
            &tx,
            &event_id,
            "reject",
            Some(agent_id),
            run_id.as_deref(),
            tool_call_id.as_deref(),
            None,
            payload.as_deref(),
        )?;
        insert_edge(
            &tx,
            &event_id,
            "input",
            &target.node_id,
            "target",
            Some(target_ref),
        )?;
        insert_edge(
            &tx,
            &event_id,
            "output",
            &target.node_id,
            "rejected_target",
            Some(target_ref),
        )?;

        update_ref_state(&tx, target_ref, &target, "rejected", &event_id)?;
        tx.commit()?;
        Ok(())
    }

    fn archive_ref_impl(
        &self,
        ref_name: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
        expected_version: Option<i64>,
    ) -> AnfsResult<()> {
        let mut conn = lock_conn(&self.inner)?;
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        require_operation_capability(&tx, "archive_ref", agent_id)?;
        let old = require_ref(&tx, ref_name)?;
        require_expected_ref_version(ref_name, &old, expected_version)?;
        validate_state_transition(&old.state, "archived")?;

        let event_id = new_event_id();
        insert_event(
            &tx,
            &event_id,
            "archive_ref",
            Some(agent_id),
            run_id.as_deref(),
            tool_call_id.as_deref(),
            None,
            Some(ref_name),
        )?;
        insert_edge(
            &tx,
            &event_id,
            "input",
            &old.node_id,
            "archived_ref",
            Some(ref_name),
        )?;
        update_ref_state(&tx, ref_name, &old, "archived", &event_id)?;
        tx.commit()?;
        Ok(())
    }

    fn verify_integrity_impl(&self) -> AnfsResult<Vec<String>> {
        let conn = lock_conn(&self.inner)?;
        verify_integrity(&conn, &self.inner.objects_dir)
    }

    fn rebuild_derived_indexes_impl(&self) -> AnfsResult<DerivedIndexRepairResult> {
        let mut conn = lock_conn(&self.inner)?;
        rebuild_derived_indexes(&mut conn, &self.inner.objects_dir)
    }

    fn repair_plan_impl(&self) -> AnfsResult<Vec<RepairPlanRow>> {
        let conn = lock_conn(&self.inner)?;
        let issues = verify_integrity(&conn, &self.inner.objects_dir)?;
        Ok(issues
            .into_iter()
            .map(|issue| {
                let (classification, action, mutable_scope) = classify_repair_issue(&issue);
                (issue, classification, action, mutable_scope)
            })
            .collect())
    }

    fn compaction_plan_impl(&self) -> AnfsResult<Vec<CompactionPlanRow>> {
        let conn = lock_conn(&self.inner)?;
        let candidates = gc_candidates(&conn, &self.inner.objects_dir, true, None, None)?;
        let unreachable_blob_bytes: i64 = candidates.iter().map(|(_, size, _)| *size).sum();
        let active_gc_pins = gc_pins(&conn, true)?.len() as i64;
        let active_retention_policies = retention_policies(&conn, true)?.len() as i64;
        let freelist_pages = sqlite_i64(&conn, "PRAGMA freelist_count")?;
        let inline_blobs = count_i64(
            &conn,
            "SELECT COUNT(*) FROM blobs WHERE storage_kind = 'inline'",
        )?;
        let inline_blob_bytes = sqlite_i64(
            &conn,
            "SELECT COALESCE(SUM(size), 0) FROM blobs WHERE storage_kind = 'inline'",
        )?;
        let orphan_objects = orphan_object_files(&conn, &self.inner.objects_dir, None)?;
        let orphan_object_bytes: i64 = orphan_objects
            .iter()
            .map(|(_hash, size, _path)| *size)
            .sum();

        let inactive_refs = count_i64(
            &conn,
            "SELECT COUNT(*) FROM refs WHERE state IN ('deleted', 'archived')",
        )?;
        let derived_rows = count_i64(&conn, "SELECT COUNT(*) FROM node_fts")?
            + count_i64(&conn, "SELECT COUNT(*) FROM node_chunk_index")?
            + count_i64(&conn, "SELECT COUNT(*) FROM materialized_working_set_files")?
            + count_i64(&conn, "SELECT COUNT(*) FROM node_embeddings")?
            + count_i64(&conn, "SELECT COUNT(*) FROM node_chunk_embeddings")?;

        let mut rows = vec![
            (
                "canonical_events".to_string(),
                count_i64(&conn, "SELECT COUNT(*) FROM events")?,
                "retain append-only event log; export signed bundles before any future archival compaction".to_string(),
                "canonical_read_only".to_string(),
            ),
            (
                "ref_audit_history".to_string(),
                count_i64(&conn, "SELECT COUNT(*) FROM ref_events")?,
                "retain audit chain; create ref-view checkpoints before any future archival compaction".to_string(),
                "canonical_read_only".to_string(),
            ),
            (
                "inactive_refs".to_string(),
                inactive_refs,
                "review deleted/archived refs before physical cleanup; they are excluded from active materialization".to_string(),
                "ref_view".to_string(),
            ),
            (
                "unreachable_blobs".to_string(),
                candidates.len() as i64,
                "run collect_garbage(include_workspaces=True, dry_run=True) and only then dry_run=False for accepted file-backed candidates".to_string(),
                "object_bytes".to_string(),
            ),
            (
                "unreachable_blob_bytes".to_string(),
                unreachable_blob_bytes,
                "byte estimate for currently unreachable blobs; file-backed candidates can be collected through GC".to_string(),
                "object_bytes".to_string(),
            ),
            (
                "orphan_object_files".to_string(),
                orphan_objects.len() as i64,
                "run clean_orphan_objects(dry_run=True) after aborted writes or imports; dry_run=False removes verified noncanonical object files only".to_string(),
                "object_store".to_string(),
            ),
            (
                "orphan_object_bytes".to_string(),
                orphan_object_bytes,
                "byte estimate for hash-shaped object files not owned by file-backed blob metadata".to_string(),
                "object_store".to_string(),
            ),
            (
                "inline_blobs".to_string(),
                inline_blobs,
                "run compact_inline_blobs(dry_run=True) to estimate moving inline bytes to CAS object files without changing nodes or refs".to_string(),
                "sqlite_blob_storage".to_string(),
            ),
            (
                "inline_blob_bytes".to_string(),
                inline_blob_bytes,
                "byte estimate for inline blob payloads currently stored in SQLite rows".to_string(),
                "sqlite_blob_storage".to_string(),
            ),
            (
                "active_gc_pins".to_string(),
                active_gc_pins,
                "review active pins before GC; pinned nodes remain reachability roots".to_string(),
                "gc_policy".to_string(),
            ),
            (
                "active_retention_policies".to_string(),
                active_retention_policies,
                "retention policies can automate bounded collect_garbage dry-runs and explicit physical collection".to_string(),
                "gc_policy".to_string(),
            ),
            (
                "derived_projection_rows".to_string(),
                derived_rows,
                "derived rows are rebuildable projections; use rebuild_derived_indexes for verified FTS/chunk-cache repair, not canonical mutation".to_string(),
                "derived".to_string(),
            ),
        ];

        let sqlite_action = if freelist_pages > 0 {
            "schedule SQLite VACUUM only after backup and writer quiescence; this plan does not run it"
        } else {
            "no SQLite freelist pressure detected"
        };
        rows.push((
            "sqlite_freelist_pages".to_string(),
            freelist_pages,
            sqlite_action.to_string(),
            "sqlite_physical".to_string(),
        ));

        Ok(rows)
    }

    fn archival_readiness_plan_impl(
        &self,
        checkpoint_id: &str,
        bundle_path: &str,
        signature_key: Option<String>,
        require_signature: bool,
    ) -> AnfsResult<Vec<ArchivalReadinessRow>> {
        let conn = lock_conn(&self.inner)?;
        let bundle_row = verify_history_archive_bundle(
            &conn,
            Path::new(bundle_path),
            signature_key.as_deref(),
            require_signature,
        )?;
        let checkpoint_row = match verify_ref_view_checkpoint(&conn, checkpoint_id) {
            Ok((
                _checkpoint_id,
                true,
                _expected_checksum,
                _actual_checksum,
                expected_count,
                _actual_count,
            )) => (
                "ref_view_checkpoint".to_string(),
                true,
                format!("ref-view checkpoint {checkpoint_id} verifies"),
                expected_count,
            ),
            Ok((
                _checkpoint_id,
                false,
                expected_checksum,
                actual_checksum,
                expected_count,
                actual_count,
            )) => (
                "ref_view_checkpoint".to_string(),
                false,
                format!(
                    "ref-view checkpoint {checkpoint_id} mismatch: expected_checksum={expected_checksum} actual_checksum={actual_checksum} expected_count={expected_count} actual_count={actual_count}"
                ),
                actual_count,
            ),
            Err(err) => (
                "ref_view_checkpoint".to_string(),
                false,
                format!("ref-view checkpoint {checkpoint_id} verification failed: {err:?}"),
                0,
            ),
        };
        let ready = bundle_row.1 && checkpoint_row.1;
        let final_detail = if ready {
            "archive bundle and replay checkpoint preconditions are satisfied; physical archival compaction is still not implemented".to_string()
        } else {
            "physical archival compaction preconditions are not satisfied".to_string()
        };
        Ok(vec![
            bundle_row,
            checkpoint_row,
            (
                "physical_archival_preconditions".to_string(),
                ready,
                final_detail,
                0,
            ),
        ])
    }

    fn compact_inline_blobs_impl(
        &self,
        dry_run: bool,
        limit: Option<i64>,
    ) -> AnfsResult<InlineBlobCompactionResult> {
        if let Some(value) = limit {
            if !(1..=10_000).contains(&value) {
                return Err(AnfsError::PolicyDenied(
                    "inline blob compaction limit must be between 1 and 10000".to_string(),
                ));
            }
        }

        with_sqlite_busy_retry(|| {
            let mut conn = lock_conn(&self.inner)?;
            let tx = conn.transaction()?;
            let candidates = if let Some(value) = limit {
                let mut stmt = tx.prepare(
                    "
                    SELECT hash, size, inline_content
                    FROM blobs
                    WHERE storage_kind = 'inline'
                    ORDER BY size DESC, hash
                    LIMIT ?1
                    ",
                )?;
                let rows = stmt.query_map(params![value], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, i64>(1)?,
                        row.get::<_, Option<Vec<u8>>>(2)?,
                    ))
                })?;
                let mut selected = Vec::new();
                for row in rows {
                    selected.push(row?);
                }
                selected
            } else {
                let mut stmt = tx.prepare(
                    "
                    SELECT hash, size, inline_content
                    FROM blobs
                    WHERE storage_kind = 'inline'
                    ORDER BY size DESC, hash
                    ",
                )?;
                let rows = stmt.query_map([], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, i64>(1)?,
                        row.get::<_, Option<Vec<u8>>>(2)?,
                    ))
                })?;
                let mut selected = Vec::new();
                for row in rows {
                    selected.push(row?);
                }
                selected
            };

            let candidate_count = candidates.len() as i64;
            let candidate_bytes: i64 = candidates.iter().map(|(_hash, size, _bytes)| *size).sum();
            let mut compacted = 0_i64;

            if !dry_run {
                for (hash, size, inline_content) in candidates {
                    let bytes = inline_content.ok_or_else(|| {
                        AnfsError::StorageCorruption(format!(
                            "inline blob {hash} has no inline_content"
                        ))
                    })?;
                    if bytes.len() as i64 != size {
                        return Err(AnfsError::StorageCorruption(format!(
                            "inline blob size mismatch before compaction: hash={hash} expected={size} actual={}",
                            bytes.len()
                        )));
                    }
                    let actual_hash = sha256_hex(&bytes);
                    if actual_hash != hash {
                        return Err(AnfsError::StorageCorruption(format!(
                            "inline blob hash mismatch before compaction: expected={hash} actual={actual_hash}"
                        )));
                    }
                    let object_path =
                        materialize_blob_file(&self.inner.objects_dir, &hash, &bytes)?;
                    tx.execute(
                        "UPDATE blobs
                         SET storage_kind = 'file',
                             storage_uri = ?2,
                             inline_content = NULL
                         WHERE hash = ?1
                           AND storage_kind = 'inline'",
                        params![hash, object_path.to_string_lossy().to_string()],
                    )?;
                    compacted += tx.changes() as i64;
                }
            }

            tx.commit()?;
            Ok((candidate_count, candidate_bytes, compacted, dry_run))
        })
    }

    fn clean_orphan_objects_impl(
        &self,
        dry_run: bool,
        limit: Option<i64>,
    ) -> AnfsResult<OrphanObjectCleanupResult> {
        let conn = lock_conn(&self.inner)?;
        let candidates = orphan_object_files(&conn, &self.inner.objects_dir, limit)?;
        let candidate_count = candidates.len() as i64;
        let candidate_bytes: i64 = candidates.iter().map(|(_hash, size, _path)| *size).sum();
        let mut removed = 0_i64;

        if !dry_run {
            for (hash, size, path) in candidates {
                let bytes = fs::read(&path)?;
                if bytes.len() as i64 != size {
                    return Err(AnfsError::StorageCorruption(format!(
                        "orphan object size changed before cleanup: hash={hash} expected={size} actual={} path={}",
                        bytes.len(),
                        path.display()
                    )));
                }
                let actual_hash = sha256_hex(&bytes);
                if actual_hash != hash {
                    return Err(AnfsError::StorageCorruption(format!(
                        "orphan object hash mismatch before cleanup: expected={hash} actual={actual_hash} path={}",
                        path.display()
                    )));
                }
                fs::remove_file(&path)?;
                removed += 1;
            }
        }

        Ok((candidate_count, candidate_bytes, removed, dry_run))
    }

    fn vacuum_database_impl(&self, dry_run: bool) -> AnfsResult<VacuumDatabaseResult> {
        let conn = lock_conn(&self.inner)?;
        let before = sqlite_i64(&conn, "PRAGMA freelist_count")?;
        if dry_run {
            return Ok((before, before, false));
        }
        conn.execute_batch("VACUUM")?;
        let after = sqlite_i64(&conn, "PRAGMA freelist_count")?;
        Ok((before, after, true))
    }

    fn schema_status_impl(&self) -> AnfsResult<Vec<SchemaStatusRow>> {
        let conn = lock_conn(&self.inner)?;
        schema_status(&conn)
    }

    fn schema_migration_plan_impl(&self) -> AnfsResult<Vec<SchemaMigrationPlanRow>> {
        let conn = lock_conn(&self.inner)?;
        schema_migration_plan(&conn)
    }

    fn apply_schema_migrations_impl(
        &self,
        dry_run: bool,
    ) -> AnfsResult<SchemaMigrationApplyResult> {
        let conn = lock_conn(&self.inner)?;
        apply_schema_migrations(&conn, dry_run)
    }

    fn require_schema_current_impl(&self) -> AnfsResult<()> {
        let conn = lock_conn(&self.inner)?;
        require_schema_current(&conn)
    }

    fn set_node_embedding_impl(
        &self,
        node_id: &str,
        model: &str,
        vector: Vec<f64>,
    ) -> AnfsResult<()> {
        let conn = lock_conn(&self.inner)?;
        set_node_embedding(&conn, node_id, model, &vector)
    }

    fn node_embedding_impl(&self, node_id: &str, model: &str) -> AnfsResult<Option<Vec<f64>>> {
        let conn = lock_conn(&self.inner)?;
        node_embedding(&conn, node_id, model)
    }

    fn vector_search_impl(
        &self,
        model: &str,
        query_vector: Vec<f64>,
        state: &str,
        limit: i64,
        policy_label_excludes: Vec<String>,
    ) -> AnfsResult<Vec<VectorSearchRow>> {
        let conn = lock_conn(&self.inner)?;
        vector_search(
            &conn,
            model,
            &query_vector,
            state,
            limit,
            &policy_label_excludes,
        )
    }

    fn set_node_chunk_embedding_impl(
        &self,
        node_id: &str,
        chunk_size: i64,
        chunk_index: i64,
        model: &str,
        vector: Vec<f64>,
    ) -> AnfsResult<()> {
        let conn = lock_conn(&self.inner)?;
        set_node_chunk_embedding(&conn, node_id, chunk_size, chunk_index, model, &vector)
    }

    fn node_chunk_embedding_impl(
        &self,
        node_id: &str,
        chunk_size: i64,
        chunk_index: i64,
        model: &str,
    ) -> AnfsResult<Option<Vec<f64>>> {
        let conn = lock_conn(&self.inner)?;
        node_chunk_embedding(&conn, node_id, chunk_size, chunk_index, model)
    }

    fn vector_search_chunks_impl(
        &self,
        model: &str,
        query_vector: Vec<f64>,
        state: &str,
        limit: i64,
        policy_label_excludes: Vec<String>,
    ) -> AnfsResult<Vec<ChunkVectorSearchRow>> {
        let conn = lock_conn(&self.inner)?;
        vector_search_chunks(
            &conn,
            model,
            &query_vector,
            state,
            limit,
            &policy_label_excludes,
        )
    }

    fn manifest_children_impl(&self, node_id: &str) -> AnfsResult<Vec<(String, String)>> {
        let conn = lock_conn(&self.inner)?;
        manifest_children(&conn, &self.inner.objects_dir, node_id)
    }

    fn manifest_child_records_impl(&self, node_id: &str) -> AnfsResult<Vec<ManifestChildRecord>> {
        let conn = lock_conn(&self.inner)?;
        manifest_child_records(&conn, &self.inner.objects_dir, node_id)
    }

    fn snapshot_namespace_impl(
        &self,
        prefix: &str,
        snapshot_ref: &str,
        agent_id: &str,
        kind: &str,
        run_id: Option<&str>,
        tool_call_id: Option<&str>,
    ) -> AnfsResult<String> {
        let mut conn = lock_conn(&self.inner)?;
        snapshot_namespace(
            &mut conn,
            &self.inner.objects_dir,
            prefix,
            snapshot_ref,
            agent_id,
            kind,
            run_id,
            tool_call_id,
        )
    }

    fn start_run_impl(
        &self,
        run_id: &str,
        agent_id: Option<String>,
        workspace_id: Option<String>,
        metadata_json: Option<String>,
    ) -> AnfsResult<RunRow> {
        let mut conn = lock_conn(&self.inner)?;
        let tx = conn.transaction()?;
        start_run(
            &tx,
            run_id,
            agent_id.as_deref(),
            workspace_id.as_deref(),
            metadata_json.as_deref(),
        )?;
        let row =
            fetch_run(&tx, run_id)?.ok_or_else(|| AnfsError::RunNotFound(run_id.to_string()))?;
        tx.commit()?;
        Ok(row)
    }

    fn finish_run_impl(
        &self,
        run_id: &str,
        state: &str,
        metadata_json: Option<String>,
    ) -> AnfsResult<RunRow> {
        let mut conn = lock_conn(&self.inner)?;
        let tx = conn.transaction()?;
        finish_run(&tx, run_id, state, metadata_json.as_deref())?;
        let row =
            fetch_run(&tx, run_id)?.ok_or_else(|| AnfsError::RunNotFound(run_id.to_string()))?;
        tx.commit()?;
        Ok(row)
    }

    fn get_run_impl(&self, run_id: &str) -> AnfsResult<RunRow> {
        let conn = lock_conn(&self.inner)?;
        fetch_run(&conn, run_id)?.ok_or_else(|| AnfsError::RunNotFound(run_id.to_string()))
    }

    fn runs_impl(
        &self,
        state: Option<String>,
        agent_id: Option<String>,
        workspace_id: Option<String>,
    ) -> AnfsResult<Vec<RunRow>> {
        let conn = lock_conn(&self.inner)?;
        runs(
            &conn,
            state.as_deref(),
            agent_id.as_deref(),
            workspace_id.as_deref(),
        )
    }

    fn policy_decisions_impl(
        &self,
        target_ref: Option<String>,
        policy: Option<String>,
        decision: Option<String>,
        reason_code: Option<String>,
    ) -> AnfsResult<Vec<(String, String)>> {
        let conn = lock_conn(&self.inner)?;
        policy_decisions(
            &conn,
            target_ref.as_deref(),
            policy.as_deref(),
            decision.as_deref(),
            reason_code.as_deref(),
        )
    }

    fn set_policy_label_impl(
        &self,
        subject_type: &str,
        subject_id: &str,
        label: &str,
        value: Option<String>,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<()> {
        let mut conn = lock_conn(&self.inner)?;
        set_policy_label(
            &mut conn,
            subject_type,
            subject_id,
            label,
            value.as_deref(),
            agent_id,
            run_id.as_deref(),
            tool_call_id.as_deref(),
        )
    }

    fn policy_labels_impl(
        &self,
        subject_type: Option<String>,
        subject_id: Option<String>,
        label: Option<String>,
        active_only: bool,
    ) -> AnfsResult<Vec<PolicyLabelRow>> {
        let conn = lock_conn(&self.inner)?;
        policy_labels(
            &conn,
            subject_type.as_deref(),
            subject_id.as_deref(),
            label.as_deref(),
            active_only,
        )
    }

    fn set_fragment_policy_label_impl(
        &self,
        node_id: &str,
        offset: i64,
        length: i64,
        label: &str,
        value: Option<String>,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<()> {
        let mut conn = lock_conn(&self.inner)?;
        set_fragment_policy_label(
            &mut conn,
            node_id,
            offset,
            length,
            label,
            value.as_deref(),
            agent_id,
            run_id.as_deref(),
            tool_call_id.as_deref(),
        )
    }

    fn fragment_policy_labels_impl(
        &self,
        node_id: Option<String>,
        label: Option<String>,
        active_only: bool,
    ) -> AnfsResult<Vec<FragmentPolicyLabelRow>> {
        let conn = lock_conn(&self.inner)?;
        fragment_policy_labels(&conn, node_id.as_deref(), label.as_deref(), active_only)
    }

    #[allow(clippy::too_many_arguments)]
    fn propagate_fragment_policy_labels_impl(
        &self,
        source_node_id: &str,
        source_offset: i64,
        source_length: i64,
        output_node_id: &str,
        output_offset: i64,
        output_length: i64,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<i64> {
        let mut conn = lock_conn(&self.inner)?;
        propagate_fragment_policy_labels_between_ranges(
            &mut conn,
            source_node_id,
            source_offset,
            source_length,
            output_node_id,
            output_offset,
            output_length,
            agent_id,
            run_id.as_deref(),
            tool_call_id.as_deref(),
        )
    }

    fn auto_propagate_fragment_policy_labels_impl(
        &self,
        source_node_id: &str,
        output_node_id: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<i64> {
        let mut conn = lock_conn(&self.inner)?;
        auto_propagate_fragment_policy_labels_by_exact_match(
            &mut conn,
            &self.inner.objects_dir,
            source_node_id,
            output_node_id,
            agent_id,
            run_id.as_deref(),
            tool_call_id.as_deref(),
        )
    }

    fn auto_propagate_fragment_policy_labels_by_normalized_scalar_impl(
        &self,
        source_node_id: &str,
        output_node_id: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<i64> {
        let mut conn = lock_conn(&self.inner)?;
        auto_propagate_fragment_policy_labels_by_normalized_scalar(
            &mut conn,
            &self.inner.objects_dir,
            source_node_id,
            output_node_id,
            agent_id,
            run_id.as_deref(),
            tool_call_id.as_deref(),
        )
    }

    fn auto_propagate_fragment_policy_labels_by_normalized_json_impl(
        &self,
        source_node_id: &str,
        output_node_id: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<i64> {
        let mut conn = lock_conn(&self.inner)?;
        auto_propagate_fragment_policy_labels_by_normalized_json(
            &mut conn,
            &self.inner.objects_dir,
            source_node_id,
            output_node_id,
            agent_id,
            run_id.as_deref(),
            tool_call_id.as_deref(),
        )
    }

    fn set_json_field_policy_label_impl(
        &self,
        node_id: &str,
        json_path: &str,
        label: &str,
        value: Option<String>,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<()> {
        let mut conn = lock_conn(&self.inner)?;
        let (_path, offset, length, _kind) =
            json_field_span(&conn, &self.inner.objects_dir, node_id, json_path)?;
        set_fragment_policy_label(
            &mut conn,
            node_id,
            offset,
            length,
            label,
            value.as_deref(),
            agent_id,
            run_id.as_deref(),
            tool_call_id.as_deref(),
        )
    }

    fn set_markdown_field_policy_label_impl(
        &self,
        node_id: &str,
        field_path: &str,
        label: &str,
        value: Option<String>,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<()> {
        let mut conn = lock_conn(&self.inner)?;
        let (_path, offset, length, _kind) =
            markdown_field_span(&conn, &self.inner.objects_dir, node_id, field_path)?;
        set_fragment_policy_label(
            &mut conn,
            node_id,
            offset,
            length,
            label,
            value.as_deref(),
            agent_id,
            run_id.as_deref(),
            tool_call_id.as_deref(),
        )
    }

    fn set_markdown_section_policy_label_impl(
        &self,
        node_id: &str,
        section_path: &str,
        label: &str,
        value: Option<String>,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<()> {
        let mut conn = lock_conn(&self.inner)?;
        let (_path, offset, length, _kind) =
            markdown_section_span(&conn, &self.inner.objects_dir, node_id, section_path)?;
        set_fragment_policy_label(
            &mut conn,
            node_id,
            offset,
            length,
            label,
            value.as_deref(),
            agent_id,
            run_id.as_deref(),
            tool_call_id.as_deref(),
        )
    }

    fn set_policy_rule_impl(
        &self,
        label: &str,
        value: Option<String>,
        effect: Option<String>,
        scope: &str,
        subject_type: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<()> {
        let mut conn = lock_conn(&self.inner)?;
        set_policy_rule(
            &mut conn,
            label,
            value.as_deref(),
            effect.as_deref(),
            scope,
            subject_type,
            agent_id,
            run_id.as_deref(),
            tool_call_id.as_deref(),
        )
    }

    fn policy_rules_impl(
        &self,
        scope: Option<String>,
        subject_type: Option<String>,
        label: Option<String>,
        active_only: bool,
    ) -> AnfsResult<Vec<PolicyRuleRow>> {
        let conn = lock_conn(&self.inner)?;
        policy_rules(
            &conn,
            scope.as_deref(),
            subject_type.as_deref(),
            label.as_deref(),
            active_only,
        )
    }

    fn set_purpose_policy_rule_impl(
        &self,
        purpose: &str,
        label: &str,
        value: Option<String>,
        effect: Option<String>,
        subject_type: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<()> {
        let mut conn = lock_conn(&self.inner)?;
        set_purpose_policy_rule(
            &mut conn,
            purpose,
            label,
            value.as_deref(),
            effect.as_deref(),
            subject_type,
            agent_id,
            run_id.as_deref(),
            tool_call_id.as_deref(),
        )
    }

    fn purpose_policy_rules_impl(
        &self,
        purpose: Option<String>,
        subject_type: Option<String>,
        label: Option<String>,
        active_only: bool,
    ) -> AnfsResult<Vec<PurposePolicyRuleRow>> {
        let conn = lock_conn(&self.inner)?;
        purpose_policy_rules(
            &conn,
            purpose.as_deref(),
            subject_type.as_deref(),
            label.as_deref(),
            active_only,
        )
    }

    fn set_agent_capability_impl(
        &self,
        target_agent_id: &str,
        capability: &str,
        effect: Option<String>,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<()> {
        let mut conn = lock_conn(&self.inner)?;
        set_agent_capability(
            &mut conn,
            target_agent_id,
            capability,
            effect.as_deref(),
            agent_id,
            run_id.as_deref(),
            tool_call_id.as_deref(),
        )
    }

    fn agent_capabilities_impl(
        &self,
        agent_id: Option<String>,
        capability: Option<String>,
        active_only: bool,
    ) -> AnfsResult<Vec<AgentCapabilityRow>> {
        let conn = lock_conn(&self.inner)?;
        agent_capabilities(
            &conn,
            agent_id.as_deref(),
            capability.as_deref(),
            active_only,
        )
    }

    fn set_purpose_capability_rule_impl(
        &self,
        purpose: &str,
        capability: &str,
        effect: Option<String>,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<()> {
        let mut conn = lock_conn(&self.inner)?;
        set_purpose_capability_rule(
            &mut conn,
            purpose,
            capability,
            effect.as_deref(),
            agent_id,
            run_id.as_deref(),
            tool_call_id.as_deref(),
        )
    }

    fn purpose_capability_rules_impl(
        &self,
        purpose: Option<String>,
        capability: Option<String>,
        active_only: bool,
    ) -> AnfsResult<Vec<PurposeCapabilityRuleRow>> {
        let conn = lock_conn(&self.inner)?;
        purpose_capability_rules(
            &conn,
            purpose.as_deref(),
            capability.as_deref(),
            active_only,
        )
    }

    fn set_operation_capability_rule_impl(
        &self,
        operation: &str,
        capability: &str,
        effect: Option<String>,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<()> {
        let mut conn = lock_conn(&self.inner)?;
        set_operation_capability_rule(
            &mut conn,
            operation,
            capability,
            effect.as_deref(),
            agent_id,
            run_id.as_deref(),
            tool_call_id.as_deref(),
        )
    }

    fn operation_capability_rules_impl(
        &self,
        operation: Option<String>,
        capability: Option<String>,
        active_only: bool,
    ) -> AnfsResult<Vec<OperationCapabilityRuleRow>> {
        let conn = lock_conn(&self.inner)?;
        operation_capability_rules(
            &conn,
            operation.as_deref(),
            capability.as_deref(),
            active_only,
        )
    }

    fn set_policy_expression_rule_impl(
        &self,
        expression_json: &str,
        effect: Option<String>,
        scope: &str,
        subject_type: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<()> {
        let mut conn = lock_conn(&self.inner)?;
        set_policy_expression_rule(
            &mut conn,
            expression_json,
            effect.as_deref(),
            scope,
            subject_type,
            agent_id,
            run_id.as_deref(),
            tool_call_id.as_deref(),
        )
    }

    fn policy_expression_rules_impl(
        &self,
        scope: Option<String>,
        subject_type: Option<String>,
        active_only: bool,
    ) -> AnfsResult<Vec<PolicyExpressionRuleRow>> {
        let conn = lock_conn(&self.inner)?;
        policy_expression_rules(
            &conn,
            scope.as_deref(),
            subject_type.as_deref(),
            active_only,
        )
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
        policy_label_excludes: Vec<String>,
        purpose: Option<String>,
    ) -> AnfsResult<Vec<QueryRefRow>> {
        let conn = lock_conn(&self.inner)?;
        query_refs(
            &conn,
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
        )
    }

    fn ref_history_impl(&self, ref_name: &str) -> AnfsResult<Vec<RefHistoryRow>> {
        let conn = lock_conn(&self.inner)?;
        ref_history(&conn, ref_name)
    }

    fn event_impl(&self, event_id: &str) -> AnfsResult<EventRecord> {
        let conn = lock_conn(&self.inner)?;
        event_record(&conn, event_id)
    }

    fn answer_evidence_coverage_impl(
        &self,
        answer_event_id: &str,
    ) -> AnfsResult<Vec<AnswerEvidenceCoverageRow>> {
        let conn = lock_conn(&self.inner)?;
        let event: Option<(String, Option<String>)> = conn
            .query_row(
                "SELECT kind, payload_json FROM events WHERE event_id = ?1",
                params![answer_event_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        let (kind, payload_json) =
            event.ok_or_else(|| AnfsError::EventNotFound(answer_event_id.to_string()))?;
        if kind != "answer" {
            return Err(AnfsError::PolicyDenied(format!(
                "event {answer_event_id} must be answer; found {kind}"
            )));
        }

        let payload_json = payload_json.ok_or_else(|| {
            AnfsError::StorageCorruption(format!(
                "answer event {answer_event_id} has missing payload"
            ))
        })?;
        let payload: serde_json::Value = serde_json::from_str(&payload_json).map_err(|err| {
            AnfsError::StorageCorruption(format!(
                "answer event {answer_event_id} has invalid payload JSON: {err}"
            ))
        })?;
        let retrieval_event_ids = payload
            .get("retrieval_event_ids")
            .and_then(|value| value.as_array())
            .ok_or_else(|| {
                AnfsError::StorageCorruption(format!(
                    "answer event {answer_event_id} has missing retrieval_event_ids array"
                ))
            })?;
        let retrieval_event_ids: Vec<String> = retrieval_event_ids
            .iter()
            .map(|value| {
                value.as_str().map(ToString::to_string).ok_or_else(|| {
                    AnfsError::StorageCorruption(format!(
                        "answer event {answer_event_id} has non-string retrieval_event_id"
                    ))
                })
            })
            .collect::<AnfsResult<_>>()?;

        let mut stmt = conn.prepare(
            "
            SELECT ee.logical_path, ee.node_id
            FROM event_edges ee
            WHERE ee.event_id = ?1
              AND ee.direction = 'input'
              AND ee.role LIKE 'answer_citation:%'
            ORDER BY CAST(substr(ee.role, 17) AS INTEGER)
            ",
        )?;
        let rows = stmt.query_map(params![answer_event_id], |row| {
            Ok((row.get::<_, Option<String>>(0)?, row.get::<_, String>(1)?))
        })?;

        let mut coverage = Vec::new();
        for row in rows {
            let (citation_ref, citation_node_id) = row?;
            let citation_ref = citation_ref.ok_or_else(|| {
                AnfsError::StorageCorruption(format!(
                    "answer event {answer_event_id} has citation edge without logical_path"
                ))
            })?;
            let mut covering_event_count = 0_i64;
            for retrieval_event_id in &retrieval_event_ids {
                covering_event_count += conn.query_row(
                    "
                    SELECT COUNT(*)
                    FROM event_edges ee
                    WHERE ee.event_id = ?1
                      AND ee.direction = 'input'
                      AND (ee.role LIKE 'query_result:%' OR ee.role LIKE 'search_result:%')
                      AND ee.node_id = ?2
                      AND ee.logical_path = ?3
                    ",
                    params![retrieval_event_id, citation_node_id, citation_ref],
                    |row| row.get::<_, i64>(0),
                )?;
            }
            coverage.push((
                citation_ref,
                citation_node_id,
                covering_event_count > 0,
                covering_event_count,
            ));
        }
        Ok(coverage)
    }

    fn answer_token_accounting_impl(
        &self,
        answer_event_id: &str,
    ) -> AnfsResult<AnswerTokenAccountingRow> {
        let conn = lock_conn(&self.inner)?;
        let event: Option<(String, Option<String>)> = conn
            .query_row(
                "SELECT kind, payload_json FROM events WHERE event_id = ?1",
                params![answer_event_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        let (kind, payload_json) =
            event.ok_or_else(|| AnfsError::EventNotFound(answer_event_id.to_string()))?;
        if kind != "answer" {
            return Err(AnfsError::PolicyDenied(format!(
                "event {answer_event_id} must be answer; found {kind}"
            )));
        }

        let payload_json = payload_json.ok_or_else(|| {
            AnfsError::StorageCorruption(format!(
                "answer event {answer_event_id} has missing payload"
            ))
        })?;
        let payload: serde_json::Value = serde_json::from_str(&payload_json).map_err(|err| {
            AnfsError::StorageCorruption(format!(
                "answer event {answer_event_id} has invalid payload JSON: {err}"
            ))
        })?;
        let token_accounting = payload.get("token_accounting").ok_or_else(|| {
            AnfsError::StorageCorruption(format!(
                "answer event {answer_event_id} has missing token_accounting"
            ))
        })?;
        let schema = token_accounting
            .get("schema")
            .and_then(|value| value.as_str())
            .ok_or_else(|| {
                AnfsError::StorageCorruption(format!(
                    "answer event {answer_event_id} has invalid token_accounting.schema"
                ))
            })?
            .to_string();
        let estimator = token_accounting
            .get("estimator")
            .and_then(|value| value.as_str())
            .ok_or_else(|| {
                AnfsError::StorageCorruption(format!(
                    "answer event {answer_event_id} has invalid token_accounting.estimator"
                ))
            })?
            .to_string();
        let answer_tokens =
            token_accounting_i64(token_accounting, answer_event_id, "answer_tokens")?;
        let citation_tokens =
            token_accounting_i64(token_accounting, answer_event_id, "citation_tokens")?;
        let total_tokens = token_accounting_i64(token_accounting, answer_event_id, "total_tokens")?;
        let citation_count =
            token_accounting_i64(token_accounting, answer_event_id, "citation_count")?;
        let retrieval_event_count =
            token_accounting_i64(token_accounting, answer_event_id, "retrieval_event_count")?;

        Ok((
            schema,
            estimator,
            answer_tokens,
            citation_tokens,
            total_tokens,
            citation_count,
            retrieval_event_count,
        ))
    }

    fn set_token_cost_profile_impl(
        &self,
        model: &str,
        input_price_micros_per_million_tokens: i64,
        output_price_micros_per_million_tokens: i64,
        prompt_overhead_tokens: i64,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<String> {
        let model = validate_token_cost_model(model)?;
        validate_nonnegative_i64(
            input_price_micros_per_million_tokens,
            "input_price_micros_per_million_tokens",
        )?;
        validate_nonnegative_i64(
            output_price_micros_per_million_tokens,
            "output_price_micros_per_million_tokens",
        )?;
        validate_nonnegative_i64(prompt_overhead_tokens, "prompt_overhead_tokens")?;

        with_sqlite_busy_retry(|| {
            let mut conn = lock_conn(&self.inner)?;
            let tx = conn.transaction()?;
            let event_id = new_event_id();
            let payload = json!({
                "model": model,
                "estimator": "ceil_bytes_div_4",
                "input_price_micros_per_million_tokens": input_price_micros_per_million_tokens,
                "output_price_micros_per_million_tokens": output_price_micros_per_million_tokens,
                "prompt_overhead_tokens": prompt_overhead_tokens,
            })
            .to_string();
            insert_event(
                &tx,
                &event_id,
                "set_token_cost_profile",
                Some(agent_id),
                run_id.as_deref(),
                tool_call_id.as_deref(),
                None,
                Some(&payload),
            )?;
            tx.execute(
                "INSERT INTO token_cost_profile_events
                 (model, estimator, input_price_micros_per_million_tokens,
                  output_price_micros_per_million_tokens, prompt_overhead_tokens,
                  event_id, agent_id, created_at)
                 VALUES (?1, 'ceil_bytes_div_4', ?2, ?3, ?4, ?5, ?6, ?7)",
                params![
                    model,
                    input_price_micros_per_million_tokens,
                    output_price_micros_per_million_tokens,
                    prompt_overhead_tokens,
                    event_id,
                    agent_id,
                    now_millis()
                ],
            )?;
            tx.commit()?;
            Ok(event_id)
        })
    }

    fn token_cost_profiles_impl(&self, active_only: bool) -> AnfsResult<Vec<TokenCostProfileRow>> {
        let conn = lock_conn(&self.inner)?;
        let sql = if active_only {
            "
            SELECT p.model,
                   p.estimator,
                   p.input_price_micros_per_million_tokens,
                   p.output_price_micros_per_million_tokens,
                   p.prompt_overhead_tokens,
                   p.event_id,
                   p.agent_id,
                   p.created_at
            FROM token_cost_profile_events p
            JOIN event_sequence es ON es.event_id = p.event_id
            WHERE NOT EXISTS (
                SELECT 1
                FROM token_cost_profile_events newer
                JOIN event_sequence newer_es ON newer_es.event_id = newer.event_id
                WHERE newer.model = p.model
                  AND newer_es.seq > es.seq
            )
            ORDER BY p.model
            "
        } else {
            "
            SELECT p.model,
                   p.estimator,
                   p.input_price_micros_per_million_tokens,
                   p.output_price_micros_per_million_tokens,
                   p.prompt_overhead_tokens,
                   p.event_id,
                   p.agent_id,
                   p.created_at
            FROM token_cost_profile_events p
            JOIN event_sequence es ON es.event_id = p.event_id
            ORDER BY p.model, es.seq
            "
        };
        let mut stmt = conn.prepare(sql)?;
        let rows = stmt.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, i64>(2)?,
                row.get::<_, i64>(3)?,
                row.get::<_, i64>(4)?,
                row.get::<_, String>(5)?,
                row.get::<_, Option<String>>(6)?,
                row.get::<_, i64>(7)?,
            ))
        })?;
        let mut profiles = Vec::new();
        for row in rows {
            profiles.push(row?);
        }
        Ok(profiles)
    }

    fn answer_cost_estimate_impl(
        &self,
        answer_event_id: &str,
        model: &str,
    ) -> AnfsResult<AnswerCostEstimateRow> {
        let model = validate_token_cost_model(model)?;
        let (
            _schema,
            estimator,
            answer_tokens,
            citation_tokens,
            _total_tokens,
            _citation_count,
            _retrieval_event_count,
        ) = self.answer_token_accounting_impl(answer_event_id)?;
        let profile = self.active_token_cost_profile(model)?.ok_or_else(|| {
            AnfsError::PolicyDenied(format!("no token cost profile for model {model}"))
        })?;
        let (
            _profile_model,
            profile_estimator,
            input_price_micros_per_million_tokens,
            output_price_micros_per_million_tokens,
            prompt_overhead_tokens,
            profile_event_id,
            _agent_id,
            _created_at,
        ) = profile;
        if profile_estimator != estimator {
            return Err(AnfsError::PolicyDenied(format!(
                "token cost profile estimator {profile_estimator} does not match answer estimator {estimator}"
            )));
        }

        let input_tokens = citation_tokens + prompt_overhead_tokens;
        let output_tokens = answer_tokens;
        let total_tokens = input_tokens + output_tokens;
        let input_cost_micros =
            cost_micros_per_million(input_tokens, input_price_micros_per_million_tokens)?;
        let output_cost_micros =
            cost_micros_per_million(output_tokens, output_price_micros_per_million_tokens)?;
        let total_cost_micros = input_cost_micros
            .checked_add(output_cost_micros)
            .ok_or_else(|| {
                AnfsError::PolicyDenied("token cost estimate overflowed i64".to_string())
            })?;

        Ok((
            model.to_string(),
            estimator,
            input_tokens,
            output_tokens,
            total_tokens,
            input_cost_micros,
            output_cost_micros,
            total_cost_micros,
            profile_event_id,
        ))
    }

    fn active_token_cost_profile(&self, model: &str) -> AnfsResult<Option<TokenCostProfileRow>> {
        let conn = lock_conn(&self.inner)?;
        conn.query_row(
            "
            SELECT p.model,
                   p.estimator,
                   p.input_price_micros_per_million_tokens,
                   p.output_price_micros_per_million_tokens,
                   p.prompt_overhead_tokens,
                   p.event_id,
                   p.agent_id,
                   p.created_at
            FROM token_cost_profile_events p
            JOIN event_sequence es ON es.event_id = p.event_id
            WHERE p.model = ?1
            ORDER BY es.seq DESC
            LIMIT 1
            ",
            params![model],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, i64>(2)?,
                    row.get::<_, i64>(3)?,
                    row.get::<_, i64>(4)?,
                    row.get::<_, String>(5)?,
                    row.get::<_, Option<String>>(6)?,
                    row.get::<_, i64>(7)?,
                ))
            },
        )
        .optional()
        .map_err(AnfsError::from)
    }

    fn answer_quote_support_impl(
        &self,
        answer_event_id: &str,
        min_quote_bytes: i64,
    ) -> AnfsResult<Vec<AnswerQuoteSupportRow>> {
        if min_quote_bytes <= 0 {
            return Err(AnfsError::PolicyDenied(
                "min_quote_bytes must be positive".to_string(),
            ));
        }
        let conn = lock_conn(&self.inner)?;
        ensure_answer_event(&conn, answer_event_id)?;

        let answer_output_nodes = answer_event_output_nodes(&conn, answer_event_id)?;
        if answer_output_nodes.len() != 1 {
            return Err(AnfsError::StorageCorruption(format!(
                "answer event {answer_event_id} must have exactly one output result edge; found {}",
                answer_output_nodes.len()
            )));
        }
        let answer_bytes =
            read_node_bytes(&conn, &self.inner.objects_dir, &answer_output_nodes[0])?;

        let mut stmt = conn.prepare(
            "
            SELECT ee.logical_path, ee.node_id
            FROM event_edges ee
            WHERE ee.event_id = ?1
              AND ee.direction = 'input'
              AND ee.role LIKE 'answer_citation:%'
            ORDER BY CAST(substr(ee.role, 17) AS INTEGER)
            ",
        )?;
        let rows = stmt.query_map(params![answer_event_id], |row| {
            Ok((row.get::<_, Option<String>>(0)?, row.get::<_, String>(1)?))
        })?;
        let mut support_rows = Vec::new();
        for row in rows {
            let (citation_ref, citation_node_id) = row?;
            let citation_ref = citation_ref.ok_or_else(|| {
                AnfsError::StorageCorruption(format!(
                    "answer event {answer_event_id} has citation edge without logical_path"
                ))
            })?;
            let citation_bytes =
                read_node_bytes(&conn, &self.inner.objects_dir, &citation_node_id)?;
            let longest = longest_common_exact_bytes(&answer_bytes, &citation_bytes) as i64;
            support_rows.push((
                citation_ref,
                citation_node_id,
                longest >= min_quote_bytes,
                longest,
            ));
        }
        Ok(support_rows)
    }

    fn events_impl(
        &self,
        after_seq: i64,
        limit: i64,
        kind: Option<String>,
        agent_id: Option<String>,
        workspace_id: Option<String>,
        run_id: Option<String>,
        created_after_ms: Option<i64>,
        created_before_ms: Option<i64>,
        payload_contains: Option<String>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<Vec<EventListRow>> {
        let conn = lock_conn(&self.inner)?;
        event_list(
            &conn,
            after_seq,
            limit,
            kind.as_deref(),
            agent_id.as_deref(),
            workspace_id.as_deref(),
            run_id.as_deref(),
            created_after_ms,
            created_before_ms,
            payload_contains.as_deref(),
            tool_call_id.as_deref(),
        )
    }

    fn node_events_impl(
        &self,
        node_id: &str,
        direction: Option<String>,
        role: Option<String>,
        after_seq: i64,
        limit: i64,
    ) -> AnfsResult<Vec<EventListRow>> {
        let conn = lock_conn(&self.inner)?;
        node_event_list(
            &conn,
            node_id,
            direction.as_deref(),
            role.as_deref(),
            after_seq,
            limit,
        )
    }

    fn lineage_nodes_impl(&self, node_id: &str, direction: &str) -> AnfsResult<Vec<String>> {
        let conn = lock_conn(&self.inner)?;
        lineage_nodes(&conn, node_id, direction)
    }

    fn lineage_graph_impl(
        &self,
        node_id: &str,
        direction: &str,
    ) -> AnfsResult<Vec<LineageGraphRow>> {
        let conn = lock_conn(&self.inner)?;
        lineage_graph(&conn, node_id, direction)
    }

    fn ref_view_at_event_impl(
        &self,
        event_id: &str,
        prefix: Option<String>,
        include_inactive: bool,
        policy_label_excludes: Vec<String>,
    ) -> AnfsResult<Vec<RefViewRow>> {
        let conn = lock_conn(&self.inner)?;
        ref_view_at_event(
            &conn,
            event_id,
            prefix.as_deref(),
            include_inactive,
            &policy_label_excludes,
        )
    }

    fn create_ref_view_checkpoint_impl(
        &self,
        event_id: Option<String>,
        prefix: Option<String>,
        include_inactive: bool,
        agent_id: &str,
    ) -> AnfsResult<RefViewCheckpointRow> {
        let mut conn = lock_conn(&self.inner)?;
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let row = create_ref_view_checkpoint(
            &tx,
            event_id.as_deref(),
            prefix.as_deref(),
            include_inactive,
            agent_id,
        )?;
        tx.commit()?;
        Ok(row)
    }

    fn ref_view_checkpoints_impl(&self) -> AnfsResult<Vec<RefViewCheckpointRow>> {
        let conn = lock_conn(&self.inner)?;
        ref_view_checkpoints(&conn)
    }

    fn verify_ref_view_checkpoint_impl(
        &self,
        checkpoint_id: &str,
    ) -> AnfsResult<RefViewCheckpointVerificationRow> {
        let conn = lock_conn(&self.inner)?;
        verify_ref_view_checkpoint(&conn, checkpoint_id)
    }

    fn materialize_ref_view_at_event_impl(
        &self,
        event_id: &str,
        output_dir: &str,
        prefix: Option<String>,
        include_inactive: bool,
        overwrite: bool,
        atomic: bool,
        policy_label_excludes: Vec<String>,
    ) -> AnfsResult<Vec<MaterializedViewRow>> {
        let conn = lock_conn(&self.inner)?;
        materialize_ref_view_at_event(
            &conn,
            &self.inner.objects_dir,
            event_id,
            Path::new(output_dir),
            prefix.as_deref(),
            include_inactive,
            overwrite,
            atomic,
            &policy_label_excludes,
        )
    }

    fn materialize_workspace_impl(
        &self,
        workspace: &str,
        output_dir: &str,
        overwrite: bool,
        atomic: bool,
        policy_label_excludes: Vec<String>,
    ) -> AnfsResult<Vec<WorktreeMaterializeRow>> {
        let conn = lock_conn(&self.inner)?;
        materialize_workspace(
            &conn,
            &self.inner.objects_dir,
            workspace,
            Path::new(output_dir),
            overwrite,
            atomic,
            &policy_label_excludes,
        )
    }

    fn commit_worktree_impl(
        &self,
        workspace: &str,
        input_dir: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
        delete_missing: bool,
        require_manifest_match: bool,
        policy_label_excludes: Vec<String>,
    ) -> AnfsResult<Vec<WorktreeCommitRow>> {
        commit_worktree(
            &self.inner,
            workspace,
            Path::new(input_dir),
            agent_id,
            run_id,
            tool_call_id,
            delete_missing,
            require_manifest_match,
            &policy_label_excludes,
        )
    }

    fn worktree_readiness_impl(
        &self,
        workspace: &str,
        input_dir: &str,
        policy_label_excludes: Vec<String>,
    ) -> AnfsResult<Vec<WorktreeReadinessRow>> {
        let conn = lock_conn(&self.inner)?;
        worktree_readiness(
            &conn,
            &self.inner.objects_dir,
            workspace,
            Path::new(input_dir),
            &policy_label_excludes,
        )
    }

    fn export_event_bundle_impl(
        &self,
        event_id: &str,
        output_dir: &str,
        include_blobs: bool,
        policy_label_excludes: Vec<String>,
        signing_key: Option<String>,
        signer_id: Option<String>,
    ) -> AnfsResult<ExportBundleResult> {
        let conn = lock_conn(&self.inner)?;
        export_event_bundle(
            &conn,
            &self.inner.objects_dir,
            event_id,
            Path::new(output_dir),
            include_blobs,
            &policy_label_excludes,
            signing_key.as_deref(),
            signer_id.as_deref(),
        )
    }

    fn export_run_bundle_impl(
        &self,
        run_id: &str,
        output_dir: &str,
        include_blobs: bool,
        policy_label_excludes: Vec<String>,
        signing_key: Option<String>,
        signer_id: Option<String>,
    ) -> AnfsResult<ExportBundleResult> {
        let conn = lock_conn(&self.inner)?;
        export_run_bundle(
            &conn,
            &self.inner.objects_dir,
            run_id,
            Path::new(output_dir),
            include_blobs,
            &policy_label_excludes,
            signing_key.as_deref(),
            signer_id.as_deref(),
        )
    }

    fn export_history_archive_impl(
        &self,
        output_dir: &str,
        include_blobs: bool,
        policy_label_excludes: Vec<String>,
        signing_key: Option<String>,
        signer_id: Option<String>,
    ) -> AnfsResult<ExportBundleResult> {
        let conn = lock_conn(&self.inner)?;
        export_history_archive(
            &conn,
            &self.inner.objects_dir,
            Path::new(output_dir),
            include_blobs,
            &policy_label_excludes,
            signing_key.as_deref(),
            signer_id.as_deref(),
        )
    }

    fn import_event_bundle_impl(
        &self,
        bundle_path: &str,
        bundle_objects_dir: Option<String>,
        policy_label_excludes: Vec<String>,
        signature_key: Option<String>,
        require_signature: bool,
    ) -> AnfsResult<ImportBundleResult> {
        let mut conn = lock_conn(&self.inner)?;
        import_event_bundle(
            &mut conn,
            &self.inner.objects_dir,
            Path::new(bundle_path),
            bundle_objects_dir.as_deref().map(Path::new),
            &policy_label_excludes,
            signature_key.as_deref(),
            require_signature,
        )
    }

    fn cache_materialized_workspace_impl(
        &self,
        workspace: &str,
        cache_root: &str,
        cache_key: Option<String>,
        overwrite: bool,
        atomic: bool,
        policy_label_excludes: Vec<String>,
    ) -> AnfsResult<CachedWorkingSetResult> {
        let mut conn = lock_conn(&self.inner)?;
        cache_materialized_workspace(
            &mut conn,
            &self.inner.objects_dir,
            workspace,
            Path::new(cache_root),
            cache_key.as_deref(),
            overwrite,
            atomic,
            &policy_label_excludes,
        )
    }

    fn workspace_diff_impl(
        &self,
        base: &str,
        workspace: &str,
    ) -> AnfsResult<Vec<(String, String, Option<String>, Option<String>)>> {
        let conn = lock_conn(&self.inner)?;
        workspace_diff(&conn, &self.inner.objects_dir, base, workspace)
    }

    fn workspace_conflicts_impl(
        &self,
        base: &str,
        workspace: &str,
    ) -> AnfsResult<Vec<(String, Option<String>, Option<String>, Option<String>)>> {
        let conn = lock_conn(&self.inner)?;
        workspace_conflicts(&conn, base, workspace)
    }

    fn merge_workspace_impl(
        &self,
        base: &str,
        workspace: &str,
        agent_id: &str,
        run_id: Option<String>,
        tool_call_id: Option<String>,
    ) -> AnfsResult<Vec<(String, String, String, Option<String>)>> {
        let mut conn = lock_conn(&self.inner)?;
        // Acquire the write lock up front (like the ref-lifecycle paths) so
        // concurrent mergers serialize on busy_timeout instead of hitting a
        // raw SQLITE_BUSY on read->write upgrade; the loser then observes the
        // advanced base and returns a clean RefConflictError.
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let conflicts = workspace_conflicts(&tx, base, workspace)?;
        if !conflicts.is_empty() {
            insert_merge_policy_decision_event(
                &tx,
                "deny",
                "workspace_conflicts",
                Some(agent_id),
                run_id.as_deref(),
                tool_call_id.as_deref(),
                base,
                workspace,
                &conflicts,
                &[],
            )?;
            tx.commit()?;
            return Err(AnfsError::RefConflict(format!(
                "workspace {workspace} has {} conflict(s) against base {base}",
                conflicts.len()
            )));
        }

        let diff = workspace_changes_since_checkout(&tx, base, workspace)?;
        let merge_target_prefix = merge_target_prefix_for_base(&tx, base)?;
        insert_merge_policy_decision_event(
            &tx,
            "allow",
            "no_workspace_conflicts",
            Some(agent_id),
            run_id.as_deref(),
            tool_call_id.as_deref(),
            base,
            workspace,
            &conflicts,
            &diff,
        )?;
        let event_id = new_event_id();
        insert_event(
            &tx,
            &event_id,
            "merge_workspace",
            Some(agent_id),
            run_id.as_deref(),
            tool_call_id.as_deref(),
            Some(workspace),
            Some(base),
        )?;

        let mut merged = Vec::new();
        for (idx, (path, status, _base_node, workspace_node)) in diff.into_iter().enumerate() {
            if status == "unchanged" {
                continue;
            }
            let base_ref = base_child_ref_name(&merge_target_prefix, &path);
            match status.as_str() {
                "added" | "modified" => {
                    let node_id = workspace_node.ok_or_else(|| {
                        AnfsError::StorageCorruption(format!(
                            "diff row {path} is {status} without workspace node"
                        ))
                    })?;
                    ensure_node_exists(&tx, &node_id)?;
                    let old = fetch_ref(&tx, &base_ref)?;
                    match old {
                        Some(old) => {
                            update_ref_node(&tx, &base_ref, &old, &node_id, "published", &event_id)?
                        }
                        None => insert_new_ref(
                            &tx,
                            &base_ref,
                            &node_id,
                            infer_ref_kind(base),
                            "published",
                            &event_id,
                        )?,
                    }
                    let input_role = format!("merge_input:{idx}");
                    let output_role = format!("merge_output:{idx}");
                    insert_edge(&tx, &event_id, "input", &node_id, &input_role, Some(&path))?;
                    insert_edge(
                        &tx,
                        &event_id,
                        "output",
                        &node_id,
                        &output_role,
                        Some(&base_ref),
                    )?;
                    merged.push((path, status, base_ref, Some(node_id)));
                }
                "deleted" => {
                    if let Some(old) = fetch_ref(&tx, &base_ref)? {
                        update_ref_state(&tx, &base_ref, &old, "archived", &event_id)?;
                        let role = format!("merge_deleted:{idx}");
                        insert_edge(
                            &tx,
                            &event_id,
                            "input",
                            &old.node_id,
                            &role,
                            Some(&base_ref),
                        )?;
                    }
                    merged.push((path, status, base_ref, None));
                }
                other => {
                    return Err(AnfsError::StorageCorruption(format!(
                        "unknown diff status {other}"
                    )))
                }
            }
        }

        tx.commit()?;
        Ok(merged)
    }

    fn gc_roots_impl(&self, include_workspaces: bool) -> AnfsResult<Vec<(String, String, String)>> {
        let conn = lock_conn(&self.inner)?;
        gc_roots(&conn, include_workspaces)
    }

    fn gc_candidates_impl(
        &self,
        include_workspaces: bool,
        older_than_ms: Option<i64>,
        limit: Option<i64>,
    ) -> AnfsResult<Vec<(String, i64, String)>> {
        let conn = lock_conn(&self.inner)?;
        gc_candidates(
            &conn,
            &self.inner.objects_dir,
            include_workspaces,
            older_than_ms,
            limit,
        )
    }

    fn collect_garbage_impl(
        &self,
        include_workspaces: bool,
        dry_run: bool,
        agent_id: &str,
        older_than_ms: Option<i64>,
        limit: Option<i64>,
    ) -> AnfsResult<Vec<GcResultRow>> {
        let mut conn = lock_conn(&self.inner)?;
        collect_garbage(
            &mut conn,
            &self.inner.objects_dir,
            include_workspaces,
            dry_run,
            agent_id,
            older_than_ms,
            limit,
            None,
        )
    }

    fn pin_ref_impl(
        &self,
        ref_name: &str,
        reason: Option<&str>,
        agent_id: &str,
        run_id: Option<&str>,
    ) -> AnfsResult<String> {
        let mut conn = lock_conn(&self.inner)?;
        pin_ref(&mut conn, ref_name, reason, agent_id, run_id)
    }

    fn unpin_gc_root_impl(
        &self,
        pin_id: &str,
        agent_id: &str,
        run_id: Option<&str>,
    ) -> AnfsResult<()> {
        let mut conn = lock_conn(&self.inner)?;
        unpin_gc_root(&mut conn, pin_id, agent_id, run_id)
    }

    fn gc_pins_impl(&self, active_only: bool) -> AnfsResult<Vec<GcPinRow>> {
        let conn = lock_conn(&self.inner)?;
        gc_pins(&conn, active_only)
    }

    fn set_retention_policy_impl(
        &self,
        policy_name: &str,
        include_workspaces: bool,
        min_age_ms: Option<i64>,
        limit: Option<i64>,
        enabled: bool,
        agent_id: &str,
        run_id: Option<&str>,
    ) -> AnfsResult<()> {
        let mut conn = lock_conn(&self.inner)?;
        set_retention_policy(
            &mut conn,
            policy_name,
            include_workspaces,
            min_age_ms,
            limit,
            enabled,
            agent_id,
            run_id,
        )
    }

    fn retention_policies_impl(&self, active_only: bool) -> AnfsResult<Vec<RetentionPolicyRow>> {
        let conn = lock_conn(&self.inner)?;
        retention_policies(&conn, active_only)
    }

    fn run_retention_policy_impl(
        &self,
        policy_name: &str,
        dry_run: bool,
        agent_id: &str,
    ) -> AnfsResult<Vec<GcResultRow>> {
        let mut conn = lock_conn(&self.inner)?;
        run_retention_policy(
            &mut conn,
            &self.inner.objects_dir,
            policy_name,
            dry_run,
            agent_id,
        )
    }
}

fn token_accounting_i64(
    token_accounting: &serde_json::Value,
    answer_event_id: &str,
    field: &str,
) -> AnfsResult<i64> {
    token_accounting
        .get(field)
        .and_then(|value| value.as_i64())
        .ok_or_else(|| {
            AnfsError::StorageCorruption(format!(
                "answer event {answer_event_id} has invalid token_accounting.{field}"
            ))
        })
}

fn validate_token_cost_model(model: &str) -> AnfsResult<&str> {
    let model = model.trim();
    if model.is_empty() {
        return Err(AnfsError::PolicyDenied(
            "token cost profile model must not be empty".to_string(),
        ));
    }
    Ok(model)
}

fn validate_nonnegative_i64(value: i64, field: &str) -> AnfsResult<()> {
    if value < 0 {
        return Err(AnfsError::PolicyDenied(format!(
            "{field} must be nonnegative"
        )));
    }
    Ok(())
}

fn cost_micros_per_million(tokens: i64, price_micros_per_million: i64) -> AnfsResult<i64> {
    validate_nonnegative_i64(tokens, "tokens")?;
    validate_nonnegative_i64(price_micros_per_million, "price_micros_per_million_tokens")?;
    let numerator = (tokens as i128)
        .checked_mul(price_micros_per_million as i128)
        .and_then(|value| value.checked_add(999_999))
        .ok_or_else(|| AnfsError::PolicyDenied("token cost estimate overflowed".to_string()))?;
    let cost = numerator / 1_000_000;
    if cost > i64::MAX as i128 {
        return Err(AnfsError::PolicyDenied(
            "token cost estimate overflowed i64".to_string(),
        ));
    }
    Ok(cost as i64)
}

fn classify_repair_issue(issue: &str) -> (String, String, String) {
    if issue.contains("node_fts")
        || issue.contains("node_chunk_index")
        || issue.contains("node_chunk_indexes")
        || issue.contains("node_chunk_embeddings")
    {
        return (
            "derived_index".to_string(),
            "run rebuild_derived_indexes(); then regenerate caller-provided embeddings if needed"
                .to_string(),
            "derived".to_string(),
        );
    }
    if issue.contains("missing file blob")
        || issue.contains("object file")
        || issue.contains("blob hash mismatch")
        || issue.contains("blob size mismatch")
    {
        return (
            "blob_storage".to_string(),
            "restore object bytes from a signed bundle, backup, or still-reachable replica; do not rewrite canonical blob metadata".to_string(),
            "object_store".to_string(),
        );
    }
    if issue.contains("policy_") || issue.contains("policy label") || issue.contains("policy rule")
    {
        return (
            "canonical_policy".to_string(),
            "manual policy audit repair required: inspect append-only policy events before restoring from bundle or backup".to_string(),
            "canonical".to_string(),
        );
    }
    if issue.contains("foreign key")
        || issue.contains("ref_event")
        || issue.contains("run_event")
        || issue.contains("event_edge")
        || issue.contains("must have exactly")
        || issue.contains("must have at least")
        || issue.contains("must reference")
        || issue.contains("audit row")
        || issue.contains("audit_rows")
        || issue.contains("chain")
        || issue.contains("current mutable")
    {
        return (
            "canonical_audit".to_string(),
            "manual canonical repair plan required: inspect event/ref/run audit history and restore from bundle or backup before mutating rows".to_string(),
            "canonical".to_string(),
        );
    }
    (
        "inspect".to_string(),
        "inspect issue and choose a repair path; no automatic canonical mutation is recommended"
            .to_string(),
        "unknown".to_string(),
    )
}

fn count_i64(conn: &Connection, sql: &str) -> AnfsResult<i64> {
    conn.query_row(sql, [], |row| row.get(0))
        .map_err(AnfsError::from)
}

fn sqlite_i64(conn: &Connection, pragma: &str) -> AnfsResult<i64> {
    conn.query_row(pragma, [], |row| row.get(0))
        .map_err(AnfsError::from)
}

fn ensure_answer_event(conn: &Connection, answer_event_id: &str) -> AnfsResult<()> {
    let kind: Option<String> = conn
        .query_row(
            "SELECT kind FROM events WHERE event_id = ?1",
            params![answer_event_id],
            |row| row.get(0),
        )
        .optional()?;
    let kind = kind.ok_or_else(|| AnfsError::EventNotFound(answer_event_id.to_string()))?;
    if kind != "answer" {
        return Err(AnfsError::PolicyDenied(format!(
            "event {answer_event_id} must be answer; found {kind}"
        )));
    }
    Ok(())
}

fn answer_event_output_nodes(conn: &Connection, answer_event_id: &str) -> AnfsResult<Vec<String>> {
    let mut stmt = conn.prepare(
        "
        SELECT ee.node_id
        FROM event_edges ee
        WHERE ee.event_id = ?1
          AND ee.direction = 'output'
          AND ee.role = 'result'
        ORDER BY ee.node_id
        ",
    )?;
    let rows = stmt.query_map(params![answer_event_id], |row| row.get::<_, String>(0))?;
    let mut node_ids = Vec::new();
    for row in rows {
        node_ids.push(row?);
    }
    Ok(node_ids)
}

fn longest_common_exact_bytes(left: &[u8], right: &[u8]) -> usize {
    let max_len = left.len().min(right.len());
    let mut low = 0_usize;
    let mut high = max_len;
    while low < high {
        let mid = (low + high).div_ceil(2);
        if has_common_exact_window(left, right, mid) {
            low = mid;
        } else {
            high = mid - 1;
        }
    }
    low
}

fn has_common_exact_window(left: &[u8], right: &[u8], len: usize) -> bool {
    if len == 0 {
        return true;
    }
    if left.len() < len || right.len() < len {
        return false;
    }
    let (shorter, longer) = if left.len() <= right.len() {
        (left, right)
    } else {
        (right, left)
    };
    let mut windows = std::collections::HashSet::new();
    for window in shorter.windows(len) {
        windows.insert(window);
    }
    longer.windows(len).any(|window| windows.contains(window))
}

fn require_operation_capability(
    conn: &Connection,
    operation: &str,
    agent_id: &str,
) -> AnfsResult<()> {
    if let Some(capability) = active_operation_capability_missing(conn, operation, agent_id)? {
        return Err(AnfsError::PolicyDenied(format!(
            "operation {operation} requires capability {capability} for agent {agent_id}"
        )));
    }
    Ok(())
}
