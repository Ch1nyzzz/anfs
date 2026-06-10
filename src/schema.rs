use rusqlite::{params, Connection, OptionalExtension};

use crate::events::{backfill_event_sequence, seed_event_sequence_allocator};
use crate::{
    now_millis, with_sqlite_busy_retry, AnfsError, AnfsResult, SchemaMigrationApplyResult,
    SchemaMigrationPlanRow, SchemaStatusRow, CURRENT_SCHEMA_NAME, CURRENT_SCHEMA_VERSION,
};

pub(crate) fn init_db(conn: &Connection) -> AnfsResult<()> {
    with_sqlite_busy_retry(|| {
        conn.execute_batch(
            "
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        PRAGMA foreign_keys=ON;
        PRAGMA busy_timeout=30000;

        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS blobs (
            hash TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            storage_kind TEXT NOT NULL,
            storage_uri TEXT,
            inline_content BLOB
        );

        CREATE TABLE IF NOT EXISTS nodes (
            node_id TEXT PRIMARY KEY,
            blob_hash TEXT NOT NULL,
            kind TEXT NOT NULL,
            media_type TEXT,
            metadata_json TEXT,
            created_at INTEGER NOT NULL,
            FOREIGN KEY(blob_hash) REFERENCES blobs(hash)
        );

        CREATE TABLE IF NOT EXISTS refs (
            ref_name TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            ref_kind TEXT NOT NULL,
            state TEXT NOT NULL,
            ref_version INTEGER NOT NULL DEFAULT 1,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id)
        );

        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            agent_id TEXT,
            run_id TEXT,
            tool_call_id TEXT,
            workspace_id TEXT,
            payload_json TEXT,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS event_sequence (
            event_id TEXT PRIMARY KEY,
            seq INTEGER NOT NULL UNIQUE,
            FOREIGN KEY(event_id) REFERENCES events(event_id)
        );

        CREATE TABLE IF NOT EXISTS event_sequence_allocations (
            seq INTEGER PRIMARY KEY AUTOINCREMENT
        );

        CREATE TABLE IF NOT EXISTS event_edges (
            event_id TEXT NOT NULL,
            direction TEXT NOT NULL,
            node_id TEXT NOT NULL,
            role TEXT NOT NULL,
            logical_path TEXT,
            PRIMARY KEY (event_id, direction, node_id, role),
            FOREIGN KEY(event_id) REFERENCES events(event_id),
            FOREIGN KEY(node_id) REFERENCES nodes(node_id)
        );

        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            agent_id TEXT,
            workspace_id TEXT,
            state TEXT NOT NULL,
            metadata_json TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            ended_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS run_events (
            run_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            old_state TEXT,
            new_state TEXT NOT NULL,
            old_metadata_json TEXT,
            new_metadata_json TEXT,
            PRIMARY KEY (run_id, event_id),
            FOREIGN KEY(run_id) REFERENCES runs(run_id),
            FOREIGN KEY(event_id) REFERENCES events(event_id)
        );

        CREATE TABLE IF NOT EXISTS ref_events (
            ref_name TEXT NOT NULL,
            event_id TEXT NOT NULL,
            old_node_id TEXT,
            new_node_id TEXT,
            old_state TEXT,
            new_state TEXT,
            PRIMARY KEY (ref_name, event_id),
            FOREIGN KEY(event_id) REFERENCES events(event_id)
        );

        CREATE TABLE IF NOT EXISTS ref_view_checkpoints (
            checkpoint_id TEXT PRIMARY KEY,
            target_event_id TEXT NOT NULL,
            target_seq INTEGER NOT NULL,
            prefix TEXT,
            include_inactive INTEGER NOT NULL,
            ref_count INTEGER NOT NULL,
            checksum TEXT NOT NULL,
            agent_id TEXT,
            created_at INTEGER NOT NULL,
            FOREIGN KEY(checkpoint_id) REFERENCES events(event_id),
            FOREIGN KEY(target_event_id) REFERENCES events(event_id),
            CHECK(include_inactive IN (0, 1)),
            CHECK(ref_count >= 0),
            CHECK(length(checksum) = 64)
        );

        CREATE TABLE IF NOT EXISTS workspace_base_refs (
            workspace_id TEXT NOT NULL,
            base_ref TEXT NOT NULL,
            logical_path TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            node_id TEXT NOT NULL,
            checkout_event_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (workspace_id, base_ref, logical_path),
            FOREIGN KEY(node_id) REFERENCES nodes(node_id),
            FOREIGN KEY(checkout_event_id) REFERENCES events(event_id)
        );

        CREATE TABLE IF NOT EXISTS workspace_path_metadata (
            workspace_id TEXT NOT NULL,
            logical_path TEXT NOT NULL,
            mode INTEGER,
            uid INTEGER,
            gid INTEGER,
            atime_ms INTEGER,
            mtime_ms INTEGER,
            ctime_ms INTEGER,
            event_id TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (workspace_id, logical_path),
            FOREIGN KEY(event_id) REFERENCES events(event_id),
            CHECK(mode IS NULL OR (mode >= 0 AND mode <= 4095)),
            CHECK(uid IS NULL OR uid >= 0),
            CHECK(gid IS NULL OR gid >= 0),
            CHECK(atime_ms IS NULL OR atime_ms >= 0),
            CHECK(mtime_ms IS NULL OR mtime_ms >= 0),
            CHECK(ctime_ms IS NULL OR ctime_ms >= 0)
        );

        CREATE TABLE IF NOT EXISTS workspace_path_locks (
            workspace_id TEXT NOT NULL,
            logical_path TEXT NOT NULL,
            lock_id TEXT NOT NULL UNIQUE,
            mode TEXT NOT NULL,
            owner_agent_id TEXT NOT NULL,
            owner_run_id TEXT,
            tool_call_id TEXT,
            acquired_at_ms INTEGER NOT NULL,
            expires_at_ms INTEGER,
            event_id TEXT NOT NULL,
            PRIMARY KEY (workspace_id, logical_path),
            FOREIGN KEY(event_id) REFERENCES events(event_id),
            CHECK(mode = 'exclusive'),
            CHECK(acquired_at_ms >= 0),
            CHECK(expires_at_ms IS NULL OR expires_at_ms > acquired_at_ms)
        );

        CREATE TABLE IF NOT EXISTS gc_blob_events (
            hash TEXT NOT NULL,
            event_id TEXT NOT NULL,
            storage_uri TEXT,
            size INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (hash, event_id),
            FOREIGN KEY(hash) REFERENCES blobs(hash),
            FOREIGN KEY(event_id) REFERENCES events(event_id)
        );

        CREATE TABLE IF NOT EXISTS gc_pin_events (
            pin_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            action TEXT NOT NULL,
            ref_name TEXT NOT NULL,
            node_id TEXT NOT NULL,
            reason TEXT,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (pin_id, event_id),
            FOREIGN KEY(event_id) REFERENCES events(event_id),
            FOREIGN KEY(node_id) REFERENCES nodes(node_id)
        );

        CREATE TABLE IF NOT EXISTS retention_policy_events (
            policy_name TEXT NOT NULL,
            effect TEXT NOT NULL,
            include_workspaces INTEGER NOT NULL,
            min_age_ms INTEGER,
            limit_count INTEGER,
            event_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (policy_name, event_id),
            FOREIGN KEY(event_id) REFERENCES events(event_id),
            CHECK(include_workspaces IN (0, 1)),
            CHECK(min_age_ms IS NULL OR min_age_ms >= 0),
            CHECK(limit_count IS NULL OR (limit_count >= 1 AND limit_count <= 10000))
        );

        CREATE TABLE IF NOT EXISTS token_cost_profile_events (
            model TEXT NOT NULL,
            estimator TEXT NOT NULL,
            input_price_micros_per_million_tokens INTEGER NOT NULL,
            output_price_micros_per_million_tokens INTEGER NOT NULL,
            prompt_overhead_tokens INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            agent_id TEXT,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (model, event_id),
            FOREIGN KEY(event_id) REFERENCES events(event_id),
            CHECK(length(trim(model)) > 0),
            CHECK(estimator = 'ceil_bytes_div_4'),
            CHECK(input_price_micros_per_million_tokens >= 0),
            CHECK(output_price_micros_per_million_tokens >= 0),
            CHECK(prompt_overhead_tokens >= 0)
        );

        CREATE TABLE IF NOT EXISTS policy_label_events (
            subject_type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            label TEXT NOT NULL,
            value TEXT,
            event_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (subject_type, subject_id, label, event_id),
            FOREIGN KEY(event_id) REFERENCES events(event_id)
        );

        CREATE TABLE IF NOT EXISTS policy_rule_events (
            scope TEXT NOT NULL,
            subject_type TEXT NOT NULL,
            label TEXT NOT NULL,
            value TEXT NOT NULL,
            effect TEXT,
            event_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (scope, subject_type, label, value, event_id),
            FOREIGN KEY(event_id) REFERENCES events(event_id)
        );

        CREATE TABLE IF NOT EXISTS policy_expression_rule_events (
            scope TEXT NOT NULL,
            subject_type TEXT NOT NULL,
            expression_json TEXT NOT NULL,
            effect TEXT,
            event_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (scope, subject_type, expression_json, event_id),
            FOREIGN KEY(event_id) REFERENCES events(event_id)
        );

        CREATE TABLE IF NOT EXISTS purpose_policy_rule_events (
            purpose TEXT NOT NULL,
            subject_type TEXT NOT NULL,
            label TEXT NOT NULL,
            value TEXT NOT NULL,
            effect TEXT,
            event_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (purpose, subject_type, label, value, event_id),
            FOREIGN KEY(event_id) REFERENCES events(event_id)
        );

        CREATE TABLE IF NOT EXISTS agent_capability_events (
            agent_id TEXT NOT NULL,
            capability TEXT NOT NULL,
            effect TEXT,
            event_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (agent_id, capability, event_id),
            FOREIGN KEY(event_id) REFERENCES events(event_id)
        );

        CREATE TABLE IF NOT EXISTS purpose_capability_rule_events (
            purpose TEXT NOT NULL,
            capability TEXT NOT NULL,
            effect TEXT,
            event_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (purpose, capability, event_id),
            FOREIGN KEY(event_id) REFERENCES events(event_id)
        );

        CREATE TABLE IF NOT EXISTS operation_capability_rule_events (
            operation TEXT NOT NULL,
            capability TEXT NOT NULL,
            effect TEXT,
            event_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (operation, capability, event_id),
            FOREIGN KEY(event_id) REFERENCES events(event_id)
        );

        CREATE TABLE IF NOT EXISTS fragment_policy_label_events (
            node_id TEXT NOT NULL,
            offset INTEGER NOT NULL,
            length INTEGER NOT NULL,
            label TEXT NOT NULL,
            value TEXT,
            event_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (node_id, offset, length, label, event_id),
            FOREIGN KEY(node_id) REFERENCES nodes(node_id),
            FOREIGN KEY(event_id) REFERENCES events(event_id),
            CHECK(offset >= 0),
            CHECK(length > 0)
        );

        CREATE TABLE IF NOT EXISTS node_chunk_indexes (
            node_id TEXT NOT NULL,
            chunk_size INTEGER NOT NULL,
            blob_hash TEXT NOT NULL,
            blob_size INTEGER NOT NULL,
            chunk_count INTEGER NOT NULL,
            indexed_at INTEGER NOT NULL,
            PRIMARY KEY (node_id, chunk_size),
            FOREIGN KEY(node_id) REFERENCES nodes(node_id),
            FOREIGN KEY(blob_hash) REFERENCES blobs(hash),
            CHECK(chunk_size > 0),
            CHECK(blob_size >= 0),
            CHECK(chunk_count >= 0)
        );

        CREATE TABLE IF NOT EXISTS node_chunk_index (
            node_id TEXT NOT NULL,
            chunk_size INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            offset INTEGER NOT NULL,
            size INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            PRIMARY KEY (node_id, chunk_size, chunk_index),
            FOREIGN KEY(node_id, chunk_size)
                REFERENCES node_chunk_indexes(node_id, chunk_size),
            CHECK(chunk_size > 0),
            CHECK(chunk_index >= 0),
            CHECK(offset >= 0),
            CHECK(size > 0)
        );

        CREATE TABLE IF NOT EXISTS materialized_working_sets (
            cache_key TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            output_dir TEXT NOT NULL,
            manifest_hash TEXT NOT NULL,
            policy_label_excludes_json TEXT NOT NULL,
            file_count INTEGER NOT NULL,
            materialized_at INTEGER NOT NULL,
            CHECK(file_count >= 0)
        );

        CREATE TABLE IF NOT EXISTS materialized_working_set_files (
            cache_key TEXT NOT NULL,
            path TEXT NOT NULL,
            ref_name TEXT NOT NULL,
            node_id TEXT NOT NULL,
            size INTEGER NOT NULL,
            PRIMARY KEY (cache_key, path),
            FOREIGN KEY(cache_key) REFERENCES materialized_working_sets(cache_key),
            FOREIGN KEY(node_id) REFERENCES nodes(node_id),
            CHECK(size >= 0)
        );

        CREATE TABLE IF NOT EXISTS node_embeddings (
            node_id TEXT NOT NULL,
            model TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            vector_json TEXT NOT NULL,
            norm REAL NOT NULL,
            indexed_at INTEGER NOT NULL,
            PRIMARY KEY (node_id, model),
            FOREIGN KEY(node_id) REFERENCES nodes(node_id),
            CHECK(dimensions > 0),
            CHECK(norm > 0.0)
        );

        CREATE TABLE IF NOT EXISTS node_chunk_embeddings (
            node_id TEXT NOT NULL,
            chunk_size INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            model TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            vector_json TEXT NOT NULL,
            norm REAL NOT NULL,
            indexed_at INTEGER NOT NULL,
            PRIMARY KEY (node_id, chunk_size, chunk_index, model),
            FOREIGN KEY(node_id, chunk_size, chunk_index)
                REFERENCES node_chunk_index(node_id, chunk_size, chunk_index),
            CHECK(chunk_size > 0),
            CHECK(chunk_index >= 0),
            CHECK(dimensions > 0),
            CHECK(norm > 0.0)
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5(
            node_id UNINDEXED,
            body
        );

        DROP TRIGGER IF EXISTS blobs_no_update;

        CREATE TRIGGER IF NOT EXISTS blobs_no_update
        BEFORE UPDATE ON blobs
        WHEN NEW.hash <> OLD.hash OR NEW.size <> OLD.size
        BEGIN
            SELECT RAISE(ABORT, 'blob CAS identity is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS blobs_no_delete
        BEFORE DELETE ON blobs
        BEGIN
            SELECT RAISE(ABORT, 'blobs are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS nodes_no_update
        BEFORE UPDATE ON nodes
        BEGIN
            SELECT RAISE(ABORT, 'nodes are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS nodes_no_delete
        BEFORE DELETE ON nodes
        BEGIN
            SELECT RAISE(ABORT, 'nodes are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS events_no_update
        BEFORE UPDATE ON events
        BEGIN
            SELECT RAISE(ABORT, 'events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS events_no_delete
        BEFORE DELETE ON events
        BEGIN
            SELECT RAISE(ABORT, 'events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS event_sequence_no_update
        BEFORE UPDATE ON event_sequence
        BEGIN
            SELECT RAISE(ABORT, 'event_sequence is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS event_sequence_no_delete
        BEFORE DELETE ON event_sequence
        BEGIN
            SELECT RAISE(ABORT, 'event_sequence is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS event_sequence_allocations_no_update
        BEFORE UPDATE ON event_sequence_allocations
        BEGIN
            SELECT RAISE(ABORT, 'event_sequence_allocations are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS event_sequence_allocations_no_delete
        BEFORE DELETE ON event_sequence_allocations
        BEGIN
            SELECT RAISE(ABORT, 'event_sequence_allocations are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS event_edges_no_update
        BEFORE UPDATE ON event_edges
        BEGIN
            SELECT RAISE(ABORT, 'event_edges are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS event_edges_no_delete
        BEFORE DELETE ON event_edges
        BEGIN
            SELECT RAISE(ABORT, 'event_edges are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS run_events_no_update
        BEFORE UPDATE ON run_events
        BEGIN
            SELECT RAISE(ABORT, 'run_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS run_events_no_delete
        BEFORE DELETE ON run_events
        BEGIN
            SELECT RAISE(ABORT, 'run_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS ref_events_no_update
        BEFORE UPDATE ON ref_events
        BEGIN
            SELECT RAISE(ABORT, 'ref_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS ref_events_no_delete
        BEFORE DELETE ON ref_events
        BEGIN
            SELECT RAISE(ABORT, 'ref_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS ref_view_checkpoints_no_update
        BEFORE UPDATE ON ref_view_checkpoints
        BEGIN
            SELECT RAISE(ABORT, 'ref_view_checkpoints are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS ref_view_checkpoints_no_delete
        BEFORE DELETE ON ref_view_checkpoints
        BEGIN
            SELECT RAISE(ABORT, 'ref_view_checkpoints are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS workspace_base_refs_no_update
        BEFORE UPDATE ON workspace_base_refs
        BEGIN
            SELECT RAISE(ABORT, 'workspace_base_refs are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS workspace_base_refs_no_delete
        BEFORE DELETE ON workspace_base_refs
        BEGIN
            SELECT RAISE(ABORT, 'workspace_base_refs are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS gc_blob_events_no_update
        BEFORE UPDATE ON gc_blob_events
        BEGIN
            SELECT RAISE(ABORT, 'gc_blob_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS gc_blob_events_no_delete
        BEFORE DELETE ON gc_blob_events
        BEGIN
            SELECT RAISE(ABORT, 'gc_blob_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS gc_pin_events_no_update
        BEFORE UPDATE ON gc_pin_events
        BEGIN
            SELECT RAISE(ABORT, 'gc_pin_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS gc_pin_events_no_delete
        BEFORE DELETE ON gc_pin_events
        BEGIN
            SELECT RAISE(ABORT, 'gc_pin_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS retention_policy_events_no_update
        BEFORE UPDATE ON retention_policy_events
        BEGIN
            SELECT RAISE(ABORT, 'retention_policy_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS retention_policy_events_no_delete
        BEFORE DELETE ON retention_policy_events
        BEGIN
            SELECT RAISE(ABORT, 'retention_policy_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS token_cost_profile_events_no_update
        BEFORE UPDATE ON token_cost_profile_events
        BEGIN
            SELECT RAISE(ABORT, 'token_cost_profile_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS token_cost_profile_events_no_delete
        BEFORE DELETE ON token_cost_profile_events
        BEGIN
            SELECT RAISE(ABORT, 'token_cost_profile_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS policy_label_events_no_update
        BEFORE UPDATE ON policy_label_events
        BEGIN
            SELECT RAISE(ABORT, 'policy_label_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS policy_label_events_no_delete
        BEFORE DELETE ON policy_label_events
        BEGIN
            SELECT RAISE(ABORT, 'policy_label_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS policy_rule_events_no_update
        BEFORE UPDATE ON policy_rule_events
        BEGIN
            SELECT RAISE(ABORT, 'policy_rule_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS policy_rule_events_no_delete
        BEFORE DELETE ON policy_rule_events
        BEGIN
            SELECT RAISE(ABORT, 'policy_rule_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS policy_expression_rule_events_no_update
        BEFORE UPDATE ON policy_expression_rule_events
        BEGIN
            SELECT RAISE(ABORT, 'policy_expression_rule_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS policy_expression_rule_events_no_delete
        BEFORE DELETE ON policy_expression_rule_events
        BEGIN
            SELECT RAISE(ABORT, 'policy_expression_rule_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS purpose_policy_rule_events_no_update
        BEFORE UPDATE ON purpose_policy_rule_events
        BEGIN
            SELECT RAISE(ABORT, 'purpose_policy_rule_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS purpose_policy_rule_events_no_delete
        BEFORE DELETE ON purpose_policy_rule_events
        BEGIN
            SELECT RAISE(ABORT, 'purpose_policy_rule_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS agent_capability_events_no_update
        BEFORE UPDATE ON agent_capability_events
        BEGIN
            SELECT RAISE(ABORT, 'agent_capability_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS agent_capability_events_no_delete
        BEFORE DELETE ON agent_capability_events
        BEGIN
            SELECT RAISE(ABORT, 'agent_capability_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS purpose_capability_rule_events_no_update
        BEFORE UPDATE ON purpose_capability_rule_events
        BEGIN
            SELECT RAISE(ABORT, 'purpose_capability_rule_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS purpose_capability_rule_events_no_delete
        BEFORE DELETE ON purpose_capability_rule_events
        BEGIN
            SELECT RAISE(ABORT, 'purpose_capability_rule_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS operation_capability_rule_events_no_update
        BEFORE UPDATE ON operation_capability_rule_events
        BEGIN
            SELECT RAISE(ABORT, 'operation_capability_rule_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS operation_capability_rule_events_no_delete
        BEFORE DELETE ON operation_capability_rule_events
        BEGIN
            SELECT RAISE(ABORT, 'operation_capability_rule_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS fragment_policy_label_events_no_update
        BEFORE UPDATE ON fragment_policy_label_events
        BEGIN
            SELECT RAISE(ABORT, 'fragment_policy_label_events are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS fragment_policy_label_events_no_delete
        BEFORE DELETE ON fragment_policy_label_events
        BEGIN
            SELECT RAISE(ABORT, 'fragment_policy_label_events are immutable');
        END;
        ",
        )
        .map_err(AnfsError::from)
    })?;
    with_sqlite_busy_retry(|| ensure_workspace_path_metadata_schema(conn))?;
    with_sqlite_busy_retry(|| apply_schema_migrations(conn, false))?;
    with_sqlite_busy_retry(|| seed_event_sequence_allocator(conn))?;
    with_sqlite_busy_retry(|| backfill_event_sequence(conn))?;
    Ok(())
}

fn ensure_workspace_path_metadata_schema(conn: &Connection) -> AnfsResult<()> {
    let mut stmt = conn.prepare("PRAGMA table_info(workspace_path_metadata)")?;
    let columns = stmt
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<Result<Vec<_>, _>>()?;
    if !columns.iter().any(|column| column == "atime_ms") {
        conn.execute(
            "ALTER TABLE workspace_path_metadata ADD COLUMN atime_ms INTEGER",
            [],
        )?;
    }
    Ok(())
}

pub(crate) fn reject_unsupported_future_schema(conn: &Connection) -> AnfsResult<()> {
    if !schema_migrations_table_exists(conn)? {
        return Ok(());
    }
    let future: Option<(i64, String)> = conn
        .query_row(
            "
            SELECT version, name
            FROM schema_migrations
            WHERE version > ?1
            ORDER BY version
            LIMIT 1
            ",
            params![CURRENT_SCHEMA_VERSION],
            |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?)),
        )
        .optional()?;
    if let Some((version, name)) = future {
        return Err(AnfsError::StorageCorruption(format!(
            "database schema version {version} ({name}) is newer than supported kernel schema version {CURRENT_SCHEMA_VERSION} ({CURRENT_SCHEMA_NAME})"
        )));
    }
    Ok(())
}

pub(crate) fn require_schema_current(conn: &Connection) -> AnfsResult<()> {
    let statuses = schema_status(conn)?;
    let unsupported: Vec<String> = statuses
        .iter()
        .filter_map(
            |(version, name, _applied_at, status)| match status.as_str() {
                "applied" | "current" => None,
                _ => Some(format!("{version}:{name}:{status}")),
            },
        )
        .collect();
    if !unsupported.is_empty() {
        return Err(AnfsError::StorageCorruption(format!(
            "schema guard failed: {}",
            unsupported.join(", ")
        )));
    }
    Ok(())
}

fn record_current_schema_migration(conn: &Connection) -> AnfsResult<()> {
    conn.execute(
        "INSERT INTO schema_migrations (version, name, applied_at)
         VALUES (?1, ?2, ?3)
         ON CONFLICT(version) DO NOTHING",
        params![CURRENT_SCHEMA_VERSION, CURRENT_SCHEMA_NAME, now_millis()],
    )?;
    Ok(())
}

pub(crate) fn schema_migration_plan(conn: &Connection) -> AnfsResult<Vec<SchemaMigrationPlanRow>> {
    let statuses = schema_status(conn)?;
    Ok(statuses
        .into_iter()
        .map(|(version, name, _applied_at, status)| {
            let action = match status.as_str() {
                "current" | "applied" => "none",
                "missing_current" => "record_current",
                "current_name_mismatch" => "manual_repair_required",
                "future_unsupported" => "unsupported_future",
                _ => "manual_repair_required",
            };
            (version, name, status, action.to_string())
        })
        .collect())
}

pub(crate) fn apply_schema_migrations(
    conn: &Connection,
    dry_run: bool,
) -> AnfsResult<SchemaMigrationApplyResult> {
    let plan = schema_migration_plan(conn)?;
    let planned = plan
        .iter()
        .filter(|(_version, _name, _status, action)| action != "none")
        .count() as i64;
    let unsupported: Vec<String> = plan
        .iter()
        .filter_map(|(version, name, status, action)| match action.as_str() {
            "unsupported_future" | "manual_repair_required" => {
                Some(format!("{version}:{name}:{status}:{action}"))
            }
            _ => None,
        })
        .collect();
    if !unsupported.is_empty() {
        return Err(AnfsError::StorageCorruption(format!(
            "schema migration plan is not safely executable: {}",
            unsupported.join(", ")
        )));
    }
    if dry_run {
        return Ok((planned, 0, true));
    }

    let mut applied = 0;
    for (_version, _name, _status, action) in plan {
        if action == "record_current" {
            record_current_schema_migration(conn)?;
            applied += 1;
        }
    }
    Ok((planned, applied, false))
}

pub(crate) fn schema_migrations(conn: &Connection) -> AnfsResult<Vec<(i64, String)>> {
    let mut stmt = conn.prepare("SELECT version, name FROM schema_migrations ORDER BY version")?;
    let rows = stmt.query_map([], |row| {
        Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
    })?;
    let mut migrations = Vec::new();
    for row in rows {
        migrations.push(row?);
    }
    Ok(migrations)
}

pub(crate) fn schema_status(conn: &Connection) -> AnfsResult<Vec<SchemaStatusRow>> {
    if !schema_migrations_table_exists(conn)? {
        return Ok(vec![(
            CURRENT_SCHEMA_VERSION,
            CURRENT_SCHEMA_NAME.to_string(),
            0,
            "missing_current".to_string(),
        )]);
    }

    let mut stmt =
        conn.prepare("SELECT version, name, applied_at FROM schema_migrations ORDER BY version")?;
    let rows = stmt.query_map([], |row| {
        Ok((
            row.get::<_, i64>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, i64>(2)?,
        ))
    })?;

    let mut statuses = Vec::new();
    let mut has_current = false;
    for row in rows {
        let (version, name, applied_at) = row?;
        let status = if version < CURRENT_SCHEMA_VERSION {
            "applied"
        } else if version == CURRENT_SCHEMA_VERSION && name == CURRENT_SCHEMA_NAME {
            has_current = true;
            "current"
        } else if version == CURRENT_SCHEMA_VERSION {
            "current_name_mismatch"
        } else {
            "future_unsupported"
        };
        statuses.push((version, name, applied_at, status.to_string()));
    }

    if !has_current {
        statuses.push((
            CURRENT_SCHEMA_VERSION,
            CURRENT_SCHEMA_NAME.to_string(),
            0,
            "missing_current".to_string(),
        ));
        statuses.sort_by_key(|(version, _, _, _)| *version);
    }

    Ok(statuses)
}

fn schema_migrations_table_exists(conn: &Connection) -> AnfsResult<bool> {
    let found: Option<i64> = conn
        .query_row(
            "
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'schema_migrations'
            LIMIT 1
            ",
            [],
            |row| row.get(0),
        )
        .optional()?;
    Ok(found.is_some())
}
