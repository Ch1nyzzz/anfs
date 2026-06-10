use rusqlite::{params, Connection};

use crate::{
    active_fragment_policy_blocks_node, active_fragment_policy_blocks_range,
    active_policy_expression_rules_block_subject, active_policy_rule_denies_label_value,
    active_purpose_policy_blocks_ref_node, active_purpose_policy_blocks_ref_node_range,
    latest_event_cte, latest_event_join, policy_label_clear_guard, AnfsError, AnfsResult,
    POLICY_EXPRESSION_RULE_EVENT_KEYS, POLICY_LABEL_EVENT_KEYS, POLICY_RULE_EVENT_KEYS,
};

// Centralized policy enforcement gates. Every read/materialization path that
// denies or hides data because of active fragment or purpose policy must route
// through one of these functions instead of calling the policy predicates in
// policy_labels.rs directly, so enforcement stays auditable in one place
// (docs/09_complexity_audit.md).

/// Deny-gate: whole-node access blocked by active fragment policy labels.
pub(crate) fn ensure_node_fragments_visible(
    conn: &Connection,
    node_id: &str,
    deny_message: impl FnOnce() -> String,
) -> AnfsResult<()> {
    if active_fragment_policy_blocks_node(conn, node_id)? {
        return Err(AnfsError::PolicyDenied(deny_message()));
    }
    Ok(())
}

/// Deny-gate: byte-range access blocked by active fragment policy labels.
pub(crate) fn ensure_node_range_visible(
    conn: &Connection,
    node_id: &str,
    offset: i64,
    length: i64,
    deny_message: impl FnOnce() -> String,
) -> AnfsResult<()> {
    if active_fragment_policy_blocks_range(conn, node_id, offset, length)? {
        return Err(AnfsError::PolicyDenied(deny_message()));
    }
    Ok(())
}

/// Deny-gate: declared purpose blocked for this ref/node pair.
pub(crate) fn ensure_purpose_allows_ref_node(
    conn: &Connection,
    purpose: &str,
    ref_name: &str,
    node_id: &str,
    deny_message: impl FnOnce() -> String,
) -> AnfsResult<()> {
    if active_purpose_policy_blocks_ref_node(conn, purpose, ref_name, node_id)? {
        return Err(AnfsError::PolicyDenied(deny_message()));
    }
    Ok(())
}

/// Deny-gate: declared purpose blocked for this ref/node byte range.
pub(crate) fn ensure_purpose_allows_ref_node_range(
    conn: &Connection,
    purpose: &str,
    ref_name: &str,
    node_id: &str,
    offset: i64,
    length: i64,
    deny_message: impl FnOnce() -> String,
) -> AnfsResult<()> {
    if active_purpose_policy_blocks_ref_node_range(
        conn, purpose, ref_name, node_id, offset, length,
    )? {
        return Err(AnfsError::PolicyDenied(deny_message()));
    }
    Ok(())
}

/// Filter-gate: scan loops skip nodes hidden by fragment policy labels.
pub(crate) fn node_fragments_hidden(conn: &Connection, node_id: &str) -> AnfsResult<bool> {
    active_fragment_policy_blocks_node(conn, node_id)
}

/// Filter-gate: scan loops skip byte ranges hidden by fragment policy labels.
pub(crate) fn node_range_hidden(
    conn: &Connection,
    node_id: &str,
    offset: i64,
    length: i64,
) -> AnfsResult<bool> {
    active_fragment_policy_blocks_range(conn, node_id, offset, length)
}

/// Filter-gate: scan loops skip ref/node pairs hidden for a declared purpose.
pub(crate) fn purpose_hides_ref_node(
    conn: &Connection,
    purpose: &str,
    ref_name: &str,
    node_id: &str,
) -> AnfsResult<bool> {
    active_purpose_policy_blocks_ref_node(conn, purpose, ref_name, node_id)
}

pub(crate) struct VisibilityPolicy<'a> {
    label_excludes: &'a [String],
}

impl<'a> VisibilityPolicy<'a> {
    pub(crate) fn new(label_excludes: &'a [String]) -> AnfsResult<Self> {
        validate_policy_label_excludes(label_excludes)?;
        Ok(Self { label_excludes })
    }

    pub(crate) fn label_excludes(&self) -> &'a [String] {
        self.label_excludes
    }

    pub(crate) fn is_unrestricted_for(&self, conn: &Connection) -> AnfsResult<bool> {
        Ok(self.label_excludes.is_empty() && !active_visibility_deny_rule_exists(conn)?)
    }

    pub(crate) fn ref_node_blocked(
        &self,
        conn: &Connection,
        ref_name: &str,
        node_id: &str,
    ) -> AnfsResult<bool> {
        ref_or_node_has_excluded_policy_label(conn, ref_name, node_id, self.label_excludes)
    }

    pub(crate) fn ref_node_label_blocked(
        &self,
        conn: &Connection,
        ref_name: &str,
        node_id: &str,
    ) -> AnfsResult<bool> {
        ref_or_node_labels_block_subject(conn, ref_name, node_id, self.label_excludes)
    }

    pub(crate) fn ensure_ref_node_visible(
        &self,
        conn: &Connection,
        ref_name: &str,
        node_id: &str,
        context: &str,
    ) -> AnfsResult<()> {
        if self.ref_node_blocked(conn, ref_name, node_id)? {
            return Err(AnfsError::PolicyDenied(format!(
                "{context} blocked by policy label on ref {ref_name} or node {node_id}"
            )));
        }
        Ok(())
    }
}

pub(crate) fn validate_policy_label_excludes(labels: &[String]) -> AnfsResult<()> {
    for label in labels {
        if label.trim().is_empty() {
            return Err(AnfsError::PolicyDenied(
                "policy_label_excludes must not contain empty labels".to_string(),
            ));
        }
    }
    Ok(())
}

pub(crate) fn ref_or_node_has_excluded_policy_label(
    conn: &Connection,
    ref_name: &str,
    node_id: &str,
    labels: &[String],
) -> AnfsResult<bool> {
    if ref_or_node_labels_block_subject(conn, ref_name, node_id, labels)?
        || active_fragment_policy_blocks_node(conn, node_id)?
    {
        return Ok(true);
    }
    Ok(false)
}

pub(crate) fn ref_or_node_labels_block_subject(
    conn: &Connection,
    ref_name: &str,
    node_id: &str,
    labels: &[String],
) -> AnfsResult<bool> {
    let policy = VisibilityPolicy::new(labels)?;
    for label in policy.label_excludes() {
        if active_policy_label_exists(conn, "ref", ref_name, label)?
            || active_policy_label_exists(conn, "node", node_id, label)?
        {
            return Ok(true);
        }
    }
    Ok(active_policy_rules_block_subject(conn, "ref", ref_name)?
        || active_policy_rules_block_subject(conn, "node", node_id)?
        || active_policy_expression_rules_block_subject(conn, "ref", ref_name)?
        || active_policy_expression_rules_block_subject(conn, "node", node_id)?)
}

fn active_policy_rules_block_subject(
    conn: &Connection,
    subject_type: &str,
    subject_id: &str,
) -> AnfsResult<bool> {
    let sql = format!(
        "
        WITH {latest}
        SELECT ple.label, ple.value
        FROM policy_label_events ple
        JOIN event_sequence es ON es.event_id = ple.event_id
        {latest_join}
        WHERE {clear_guard}
        ORDER BY ple.label, ple.value
        ",
        latest = latest_event_cte(
            "latest_value",
            "policy_label_events",
            "ple",
            POLICY_LABEL_EVENT_KEYS,
            "WHERE ple.subject_type = ?1
               AND ple.subject_id = ?2
               AND ple.value IS NOT NULL",
        ),
        latest_join = latest_event_join("latest_value", "l", "ple", "es", POLICY_LABEL_EVENT_KEYS),
        clear_guard = policy_label_clear_guard("ple", "es"),
    );
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(params![subject_type, subject_id], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    for row in rows {
        let (label, value) = row?;
        if active_policy_rule_denies_label_value(conn, subject_type, &label, &value)? {
            return Ok(true);
        }
    }
    Ok(false)
}

fn active_visibility_deny_rule_exists(conn: &Connection) -> AnfsResult<bool> {
    let label_rule_sql = format!(
        "
        WITH {latest}
        SELECT COUNT(*)
        FROM policy_rule_events pre
        JOIN event_sequence es ON es.event_id = pre.event_id
        {latest_join}
        WHERE pre.effect = 'deny'
        ",
        latest = latest_event_cte(
            "latest",
            "policy_rule_events",
            "pre",
            POLICY_RULE_EVENT_KEYS,
            "WHERE pre.scope = 'visibility'",
        ),
        latest_join = latest_event_join("latest", "l", "pre", "es", POLICY_RULE_EVENT_KEYS),
    );
    let label_rule_count: i64 = conn.query_row(&label_rule_sql, [], |row| row.get(0))?;
    if label_rule_count > 0 {
        return Ok(true);
    }
    let expression_rule_sql = format!(
        "
        WITH {latest}
        SELECT COUNT(*)
        FROM policy_expression_rule_events pere
        JOIN event_sequence es ON es.event_id = pere.event_id
        {latest_join}
        WHERE pere.effect = 'deny'
        ",
        latest = latest_event_cte(
            "latest",
            "policy_expression_rule_events",
            "pere",
            POLICY_EXPRESSION_RULE_EVENT_KEYS,
            "WHERE pere.scope = 'visibility'",
        ),
        latest_join = latest_event_join(
            "latest",
            "l",
            "pere",
            "es",
            POLICY_EXPRESSION_RULE_EVENT_KEYS
        ),
    );
    let expression_rule_count: i64 = conn.query_row(&expression_rule_sql, [], |row| row.get(0))?;
    Ok(expression_rule_count > 0)
}

fn active_policy_label_exists(
    conn: &Connection,
    subject_type: &str,
    subject_id: &str,
    label: &str,
) -> AnfsResult<bool> {
    let sql = format!(
        "
        WITH {latest}
        SELECT COUNT(*)
        FROM policy_label_events ple
        JOIN event_sequence es ON es.event_id = ple.event_id
        {latest_join}
        WHERE {clear_guard}
        ",
        latest = latest_event_cte(
            "latest_value",
            "policy_label_events",
            "ple",
            POLICY_LABEL_EVENT_KEYS,
            "WHERE ple.subject_type = ?1
               AND ple.subject_id = ?2
               AND ple.label = ?3
               AND ple.value IS NOT NULL",
        ),
        latest_join = latest_event_join("latest_value", "l", "ple", "es", POLICY_LABEL_EVENT_KEYS),
        clear_guard = policy_label_clear_guard("ple", "es"),
    );
    let count: i64 = conn.query_row(&sql, params![subject_type, subject_id, label], |row| {
        row.get(0)
    })?;
    Ok(count > 0)
}
