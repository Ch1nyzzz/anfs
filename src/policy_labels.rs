use rusqlite::{params, Connection, OptionalExtension, Transaction};
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::path::Path;

use crate::{
    ensure_node_exists, insert_edge, insert_event, new_event_id, now_millis, read_node_bytes,
    require_ref, AgentCapabilityRow, AnfsError, AnfsResult, FragmentPolicyLabelRow,
    OperationCapabilityRuleRow, PolicyExpressionRuleRow, PolicyLabelRow, PolicyRuleRow,
    PurposeCapabilityRuleRow, PurposePolicyRuleRow,
};

// The three SQL-fragment builders below are the single source of the
// latest-active-value semantics: an event row is "active" when it carries the
// highest event_sequence seq within its key group (and, for policy labels, no
// later clear event exists). Every "latest event per group wins" query in this
// crate must be assembled from these builders instead of hand-writing the
// pattern.

/// CTE body selecting the highest event sequence per `keys` group of `table`.
/// `where_clause` is raw SQL inserted between the event_sequence join and the
/// GROUP BY (extra JOINs and/or a WHERE; may be empty).
pub(crate) fn latest_event_cte(
    cte_name: &str,
    table: &str,
    alias: &str,
    keys: &[&str],
    where_clause: &str,
) -> String {
    let key_list = keys
        .iter()
        .map(|key| format!("{alias}.{key}"))
        .collect::<Vec<_>>()
        .join(", ");
    format!(
        "{cte_name} AS (
            SELECT {key_list}, MAX(es.seq) AS seq
            FROM {table} {alias}
            JOIN event_sequence es ON es.event_id = {alias}.event_id
            {where_clause}
            GROUP BY {key_list}
        )"
    )
}

/// JOIN clause matching `alias` rows against the `cte_name` CTE on `keys` and seq.
pub(crate) fn latest_event_join(
    cte_name: &str,
    cte_alias: &str,
    alias: &str,
    seq_alias: &str,
    keys: &[&str],
) -> String {
    let mut conditions = keys
        .iter()
        .map(|key| format!("{cte_alias}.{key} = {alias}.{key}"))
        .collect::<Vec<_>>();
    conditions.push(format!("{cte_alias}.seq = {seq_alias}.seq"));
    format!(
        "JOIN {cte_name} {cte_alias} ON {}",
        conditions.join(" AND ")
    )
}

/// NOT EXISTS guard: row is dropped when a later clear event (value IS NULL) exists.
pub(crate) fn policy_label_clear_guard(alias: &str, seq_alias: &str) -> String {
    format!(
        "NOT EXISTS (
            SELECT 1
            FROM policy_label_events clear
            JOIN event_sequence clear_seq ON clear_seq.event_id = clear.event_id
            WHERE clear.subject_type = {alias}.subject_type
              AND clear.subject_id = {alias}.subject_id
              AND clear.label = {alias}.label
              AND clear.value IS NULL
              AND clear_seq.seq > {seq_alias}.seq
        )"
    )
}

/// Latest-wins key columns per event table (shared across query sites).
pub(crate) const POLICY_LABEL_EVENT_KEYS: &[&str] =
    &["subject_type", "subject_id", "label", "value"];
pub(crate) const POLICY_RULE_EVENT_KEYS: &[&str] = &["scope", "subject_type", "label", "value"];
pub(crate) const POLICY_EXPRESSION_RULE_EVENT_KEYS: &[&str] =
    &["scope", "subject_type", "expression_json"];
pub(crate) const FRAGMENT_POLICY_LABEL_EVENT_KEYS: &[&str] =
    &["node_id", "offset", "length", "label"];

pub(crate) fn set_policy_label(
    conn: &mut Connection,
    subject_type: &str,
    subject_id: &str,
    label: &str,
    value: Option<&str>,
    agent_id: &str,
    run_id: Option<&str>,
    tool_call_id: Option<&str>,
) -> AnfsResult<()> {
    if label.trim().is_empty() {
        return Err(AnfsError::PolicyDenied(
            "policy label must not be empty".to_string(),
        ));
    }
    let edge_node_id = resolve_policy_label_subject(conn, subject_type, subject_id)?;
    let tx = conn.transaction()?;
    insert_policy_label_event(
        &tx,
        subject_type,
        subject_id,
        &edge_node_id,
        label,
        value,
        agent_id,
        run_id,
        tool_call_id,
        None,
    )?;
    tx.commit()?;
    Ok(())
}

fn insert_policy_label_event(
    tx: &Transaction<'_>,
    subject_type: &str,
    subject_id: &str,
    edge_node_id: &str,
    label: &str,
    value: Option<&str>,
    agent_id: &str,
    run_id: Option<&str>,
    tool_call_id: Option<&str>,
    propagated_from_node_id: Option<&str>,
) -> AnfsResult<()> {
    let event_id = new_event_id();
    let action = if value.is_some() { "set" } else { "clear" };
    let mut payload = json!({
        "subject_type": subject_type,
        "subject_id": subject_id,
        "label": label,
        "value": value,
        "action": action,
    });
    if let Some(source_node_id) = propagated_from_node_id {
        payload["propagated_from_node_id"] = json!(source_node_id);
    }
    let payload = payload.to_string();
    insert_event(
        tx,
        &event_id,
        "policy_label",
        Some(agent_id),
        run_id,
        tool_call_id,
        None,
        Some(&payload),
    )?;
    insert_edge(
        tx,
        &event_id,
        "input",
        edge_node_id,
        "policy_label_subject",
        Some(subject_id),
    )?;
    tx.execute(
        "INSERT INTO policy_label_events
         (subject_type, subject_id, label, value, event_id, created_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        params![
            subject_type,
            subject_id,
            label,
            value,
            event_id,
            now_millis()
        ],
    )?;
    Ok(())
}

pub(crate) fn policy_labels(
    conn: &Connection,
    subject_type: Option<&str>,
    subject_id: Option<&str>,
    label: Option<&str>,
    active_only: bool,
) -> AnfsResult<Vec<PolicyLabelRow>> {
    if active_only {
        let sql = format!(
            "
            WITH {latest}
            SELECT ple.subject_type, ple.subject_id, ple.label, ple.value,
                   ple.event_id, e.agent_id, ple.created_at
            FROM policy_label_events ple
            JOIN event_sequence es ON es.event_id = ple.event_id
            {latest_join}
            JOIN events e ON e.event_id = ple.event_id
            WHERE {clear_guard}
            ORDER BY ple.subject_type, ple.subject_id, ple.label, ple.value
            ",
            latest = latest_event_cte(
                "latest_value",
                "policy_label_events",
                "ple",
                POLICY_LABEL_EVENT_KEYS,
                "WHERE (?1 IS NULL OR ple.subject_type = ?1)
                   AND (?2 IS NULL OR ple.subject_id = ?2)
                   AND (?3 IS NULL OR ple.label = ?3)
                   AND ple.value IS NOT NULL",
            ),
            latest_join =
                latest_event_join("latest_value", "l", "ple", "es", POLICY_LABEL_EVENT_KEYS),
            clear_guard = policy_label_clear_guard("ple", "es"),
        );
        let mut stmt = conn.prepare(&sql)?;
        let rows = stmt.query_map(params![subject_type, subject_id, label], policy_label_row)?;
        collect_policy_label_rows(rows)
    } else {
        let mut stmt = conn.prepare(
            "
            SELECT ple.subject_type, ple.subject_id, ple.label, ple.value,
                   ple.event_id, e.agent_id, ple.created_at
            FROM policy_label_events ple
            JOIN event_sequence es ON es.event_id = ple.event_id
            JOIN events e ON e.event_id = ple.event_id
            WHERE (?1 IS NULL OR ple.subject_type = ?1)
              AND (?2 IS NULL OR ple.subject_id = ?2)
              AND (?3 IS NULL OR ple.label = ?3)
            ORDER BY es.seq, ple.subject_type, ple.subject_id, ple.label
            ",
        )?;
        let rows = stmt.query_map(params![subject_type, subject_id, label], policy_label_row)?;
        collect_policy_label_rows(rows)
    }
}

pub(crate) fn set_policy_rule(
    conn: &mut Connection,
    label: &str,
    value: Option<&str>,
    effect: Option<&str>,
    scope: &str,
    subject_type: &str,
    agent_id: &str,
    run_id: Option<&str>,
    tool_call_id: Option<&str>,
) -> AnfsResult<()> {
    let label = validate_policy_rule_label(label)?;
    let value = validate_policy_rule_value(value)?;
    let effect = validate_policy_rule_effect(effect)?;
    let scope = validate_policy_rule_scope(scope)?;
    let subject_type = validate_policy_rule_subject_type(subject_type)?;
    let tx = conn.transaction()?;
    let event_id = new_event_id();
    let action = if effect.is_some() { "set" } else { "clear" };
    let payload = json!({
        "scope": scope,
        "subject_type": subject_type,
        "label": label,
        "value": value,
        "effect": effect,
        "action": action,
    })
    .to_string();
    insert_event(
        &tx,
        &event_id,
        "policy_rule",
        Some(agent_id),
        run_id,
        tool_call_id,
        None,
        Some(&payload),
    )?;
    tx.execute(
        "INSERT INTO policy_rule_events
         (scope, subject_type, label, value, effect, event_id, created_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        params![
            scope,
            subject_type,
            label,
            value,
            effect,
            event_id,
            now_millis()
        ],
    )?;
    tx.commit()?;
    Ok(())
}

pub(crate) fn policy_rules(
    conn: &Connection,
    scope: Option<&str>,
    subject_type: Option<&str>,
    label: Option<&str>,
    active_only: bool,
) -> AnfsResult<Vec<PolicyRuleRow>> {
    if active_only {
        let sql = format!(
            "
            WITH {latest}
            SELECT pre.scope, pre.subject_type, pre.label, pre.value, pre.effect,
                   pre.event_id, e.agent_id, pre.created_at
            FROM policy_rule_events pre
            JOIN event_sequence es ON es.event_id = pre.event_id
            {latest_join}
            JOIN events e ON e.event_id = pre.event_id
            WHERE pre.effect IS NOT NULL
            ORDER BY pre.scope, pre.subject_type, pre.label, pre.value
            ",
            latest = latest_event_cte(
                "latest",
                "policy_rule_events",
                "pre",
                POLICY_RULE_EVENT_KEYS,
                "WHERE (?1 IS NULL OR pre.scope = ?1)
                   AND (?2 IS NULL OR pre.subject_type = ?2)
                   AND (?3 IS NULL OR pre.label = ?3)",
            ),
            latest_join = latest_event_join("latest", "l", "pre", "es", POLICY_RULE_EVENT_KEYS),
        );
        let mut stmt = conn.prepare(&sql)?;
        let rows = stmt.query_map(params![scope, subject_type, label], policy_rule_row)?;
        collect_policy_rule_rows(rows)
    } else {
        let mut stmt = conn.prepare(
            "
            SELECT pre.scope, pre.subject_type, pre.label, pre.value, pre.effect,
                   pre.event_id, e.agent_id, pre.created_at
            FROM policy_rule_events pre
            JOIN event_sequence es ON es.event_id = pre.event_id
            JOIN events e ON e.event_id = pre.event_id
            WHERE (?1 IS NULL OR pre.scope = ?1)
              AND (?2 IS NULL OR pre.subject_type = ?2)
              AND (?3 IS NULL OR pre.label = ?3)
            ORDER BY es.seq, pre.scope, pre.subject_type, pre.label, pre.value
            ",
        )?;
        let rows = stmt.query_map(params![scope, subject_type, label], policy_rule_row)?;
        collect_policy_rule_rows(rows)
    }
}

pub(crate) fn set_policy_expression_rule(
    conn: &mut Connection,
    expression_json: &str,
    effect: Option<&str>,
    scope: &str,
    subject_type: &str,
    agent_id: &str,
    run_id: Option<&str>,
    tool_call_id: Option<&str>,
) -> AnfsResult<()> {
    let expression_json = validate_policy_expression_json(expression_json)?;
    let effect = validate_policy_rule_effect(effect)?;
    let scope = validate_policy_rule_scope(scope)?;
    let subject_type = validate_policy_rule_subject_type(subject_type)?;
    let expression_value = parse_policy_expression(expression_json)?;
    let tx = conn.transaction()?;
    let event_id = new_event_id();
    let action = if effect.is_some() { "set" } else { "clear" };
    let payload = json!({
        "scope": scope,
        "subject_type": subject_type,
        "expression": expression_value,
        "effect": effect,
        "action": action,
    })
    .to_string();
    insert_event(
        &tx,
        &event_id,
        "policy_expression_rule",
        Some(agent_id),
        run_id,
        tool_call_id,
        None,
        Some(&payload),
    )?;
    tx.execute(
        "INSERT INTO policy_expression_rule_events
         (scope, subject_type, expression_json, effect, event_id, created_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        params![
            scope,
            subject_type,
            expression_json,
            effect,
            event_id,
            now_millis()
        ],
    )?;
    tx.commit()?;
    Ok(())
}

pub(crate) fn policy_expression_rules(
    conn: &Connection,
    scope: Option<&str>,
    subject_type: Option<&str>,
    active_only: bool,
) -> AnfsResult<Vec<PolicyExpressionRuleRow>> {
    if active_only {
        let sql = format!(
            "
            WITH {latest}
            SELECT pere.scope, pere.subject_type, pere.expression_json, pere.effect,
                   pere.event_id, e.agent_id, pere.created_at
            FROM policy_expression_rule_events pere
            JOIN event_sequence es ON es.event_id = pere.event_id
            {latest_join}
            JOIN events e ON e.event_id = pere.event_id
            WHERE pere.effect IS NOT NULL
            ORDER BY pere.scope, pere.subject_type, pere.expression_json
            ",
            latest = latest_event_cte(
                "latest",
                "policy_expression_rule_events",
                "pere",
                POLICY_EXPRESSION_RULE_EVENT_KEYS,
                "WHERE (?1 IS NULL OR pere.scope = ?1)
                   AND (?2 IS NULL OR pere.subject_type = ?2)",
            ),
            latest_join = latest_event_join(
                "latest",
                "l",
                "pere",
                "es",
                POLICY_EXPRESSION_RULE_EVENT_KEYS
            ),
        );
        let mut stmt = conn.prepare(&sql)?;
        let rows = stmt.query_map(params![scope, subject_type], policy_expression_rule_row)?;
        collect_policy_expression_rule_rows(rows)
    } else {
        let mut stmt = conn.prepare(
            "
            SELECT pere.scope, pere.subject_type, pere.expression_json, pere.effect,
                   pere.event_id, e.agent_id, pere.created_at
            FROM policy_expression_rule_events pere
            JOIN event_sequence es ON es.event_id = pere.event_id
            JOIN events e ON e.event_id = pere.event_id
            WHERE (?1 IS NULL OR pere.scope = ?1)
              AND (?2 IS NULL OR pere.subject_type = ?2)
            ORDER BY es.seq, pere.scope, pere.subject_type, pere.expression_json
            ",
        )?;
        let rows = stmt.query_map(params![scope, subject_type], policy_expression_rule_row)?;
        collect_policy_expression_rule_rows(rows)
    }
}

pub(crate) fn set_purpose_policy_rule(
    conn: &mut Connection,
    purpose: &str,
    label: &str,
    value: Option<&str>,
    effect: Option<&str>,
    subject_type: &str,
    agent_id: &str,
    run_id: Option<&str>,
    tool_call_id: Option<&str>,
) -> AnfsResult<()> {
    let purpose = validate_purpose_policy_rule_purpose(purpose)?;
    let label = validate_policy_rule_label(label)?;
    let value = validate_policy_rule_value(value)?;
    let effect = validate_policy_rule_effect(effect)?;
    let subject_type = validate_purpose_policy_rule_subject_type(subject_type)?;
    let tx = conn.transaction()?;
    let event_id = new_event_id();
    let action = if effect.is_some() { "set" } else { "clear" };
    let payload = json!({
        "purpose": purpose,
        "subject_type": subject_type,
        "label": label,
        "value": value,
        "effect": effect,
        "action": action,
    })
    .to_string();
    insert_event(
        &tx,
        &event_id,
        "purpose_policy_rule",
        Some(agent_id),
        run_id,
        tool_call_id,
        None,
        Some(&payload),
    )?;
    tx.execute(
        "INSERT INTO purpose_policy_rule_events
         (purpose, subject_type, label, value, effect, event_id, created_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        params![
            purpose,
            subject_type,
            label,
            value,
            effect,
            event_id,
            now_millis()
        ],
    )?;
    tx.commit()?;
    Ok(())
}

pub(crate) fn purpose_policy_rules(
    conn: &Connection,
    purpose: Option<&str>,
    subject_type: Option<&str>,
    label: Option<&str>,
    active_only: bool,
) -> AnfsResult<Vec<PurposePolicyRuleRow>> {
    if active_only {
        let sql = format!(
            "
            WITH {latest}
            SELECT ppre.purpose, ppre.subject_type, ppre.label, ppre.value, ppre.effect,
                   ppre.event_id, e.agent_id, ppre.created_at
            FROM purpose_policy_rule_events ppre
            JOIN event_sequence es ON es.event_id = ppre.event_id
            {latest_join}
            JOIN events e ON e.event_id = ppre.event_id
            WHERE ppre.effect IS NOT NULL
            ORDER BY ppre.purpose, ppre.subject_type, ppre.label, ppre.value
            ",
            latest = latest_event_cte(
                "latest",
                "purpose_policy_rule_events",
                "ppre",
                &["purpose", "subject_type", "label", "value"],
                "WHERE (?1 IS NULL OR ppre.purpose = ?1)
                   AND (?2 IS NULL OR ppre.subject_type = ?2)
                   AND (?3 IS NULL OR ppre.label = ?3)",
            ),
            latest_join = latest_event_join(
                "latest",
                "l",
                "ppre",
                "es",
                &["purpose", "subject_type", "label", "value"],
            ),
        );
        let mut stmt = conn.prepare(&sql)?;
        let rows = stmt.query_map(
            params![purpose, subject_type, label],
            purpose_policy_rule_row,
        )?;
        collect_purpose_policy_rule_rows(rows)
    } else {
        let mut stmt = conn.prepare(
            "
            SELECT ppre.purpose, ppre.subject_type, ppre.label, ppre.value, ppre.effect,
                   ppre.event_id, e.agent_id, ppre.created_at
            FROM purpose_policy_rule_events ppre
            JOIN event_sequence es ON es.event_id = ppre.event_id
            JOIN events e ON e.event_id = ppre.event_id
            WHERE (?1 IS NULL OR ppre.purpose = ?1)
              AND (?2 IS NULL OR ppre.subject_type = ?2)
              AND (?3 IS NULL OR ppre.label = ?3)
            ORDER BY es.seq, ppre.purpose, ppre.subject_type, ppre.label, ppre.value
            ",
        )?;
        let rows = stmt.query_map(
            params![purpose, subject_type, label],
            purpose_policy_rule_row,
        )?;
        collect_purpose_policy_rule_rows(rows)
    }
}

pub(crate) fn set_agent_capability(
    conn: &mut Connection,
    target_agent_id: &str,
    capability: &str,
    effect: Option<&str>,
    agent_id: &str,
    run_id: Option<&str>,
    tool_call_id: Option<&str>,
) -> AnfsResult<()> {
    let target_agent_id = validate_capability_agent_id(target_agent_id)?;
    let capability = validate_capability_name(capability)?;
    let effect = validate_agent_capability_effect(effect)?;
    let tx = conn.transaction()?;
    let event_id = new_event_id();
    let action = if effect.is_some() { "grant" } else { "revoke" };
    let payload = json!({
        "target_agent_id": target_agent_id,
        "capability": capability,
        "effect": effect,
        "action": action,
    })
    .to_string();
    insert_event(
        &tx,
        &event_id,
        "agent_capability",
        Some(agent_id),
        run_id,
        tool_call_id,
        None,
        Some(&payload),
    )?;
    tx.execute(
        "INSERT INTO agent_capability_events
         (agent_id, capability, effect, event_id, created_at)
         VALUES (?1, ?2, ?3, ?4, ?5)",
        params![target_agent_id, capability, effect, event_id, now_millis()],
    )?;
    tx.commit()?;
    Ok(())
}

pub(crate) fn agent_capabilities(
    conn: &Connection,
    agent_id: Option<&str>,
    capability: Option<&str>,
    active_only: bool,
) -> AnfsResult<Vec<AgentCapabilityRow>> {
    if active_only {
        let sql = format!(
            "
            WITH {latest}
            SELECT ace.agent_id, ace.capability, ace.effect, ace.event_id,
                   e.agent_id, ace.created_at
            FROM agent_capability_events ace
            JOIN event_sequence es ON es.event_id = ace.event_id
            {latest_join}
            JOIN events e ON e.event_id = ace.event_id
            WHERE ace.effect IS NOT NULL
            ORDER BY ace.agent_id, ace.capability
            ",
            latest = latest_event_cte(
                "latest",
                "agent_capability_events",
                "ace",
                &["agent_id", "capability"],
                "WHERE (?1 IS NULL OR ace.agent_id = ?1)
                   AND (?2 IS NULL OR ace.capability = ?2)",
            ),
            latest_join =
                latest_event_join("latest", "l", "ace", "es", &["agent_id", "capability"]),
        );
        let mut stmt = conn.prepare(&sql)?;
        let rows = stmt.query_map(params![agent_id, capability], agent_capability_row)?;
        collect_agent_capability_rows(rows)
    } else {
        let mut stmt = conn.prepare(
            "
            SELECT ace.agent_id, ace.capability, ace.effect, ace.event_id,
                   e.agent_id, ace.created_at
            FROM agent_capability_events ace
            JOIN event_sequence es ON es.event_id = ace.event_id
            JOIN events e ON e.event_id = ace.event_id
            WHERE (?1 IS NULL OR ace.agent_id = ?1)
              AND (?2 IS NULL OR ace.capability = ?2)
            ORDER BY es.seq, ace.agent_id, ace.capability
            ",
        )?;
        let rows = stmt.query_map(params![agent_id, capability], agent_capability_row)?;
        collect_agent_capability_rows(rows)
    }
}

pub(crate) fn set_purpose_capability_rule(
    conn: &mut Connection,
    purpose: &str,
    capability: &str,
    effect: Option<&str>,
    agent_id: &str,
    run_id: Option<&str>,
    tool_call_id: Option<&str>,
) -> AnfsResult<()> {
    let purpose = validate_purpose_policy_rule_purpose(purpose)?;
    let capability = validate_capability_name(capability)?;
    let effect = validate_purpose_capability_rule_effect(effect)?;
    let tx = conn.transaction()?;
    let event_id = new_event_id();
    let action = if effect.is_some() { "set" } else { "clear" };
    let payload = json!({
        "purpose": purpose,
        "capability": capability,
        "effect": effect,
        "action": action,
    })
    .to_string();
    insert_event(
        &tx,
        &event_id,
        "purpose_capability_rule",
        Some(agent_id),
        run_id,
        tool_call_id,
        None,
        Some(&payload),
    )?;
    tx.execute(
        "INSERT INTO purpose_capability_rule_events
         (purpose, capability, effect, event_id, created_at)
         VALUES (?1, ?2, ?3, ?4, ?5)",
        params![purpose, capability, effect, event_id, now_millis()],
    )?;
    tx.commit()?;
    Ok(())
}

pub(crate) fn purpose_capability_rules(
    conn: &Connection,
    purpose: Option<&str>,
    capability: Option<&str>,
    active_only: bool,
) -> AnfsResult<Vec<PurposeCapabilityRuleRow>> {
    if active_only {
        let sql = format!(
            "
            WITH {latest}
            SELECT pcre.purpose, pcre.capability, pcre.effect, pcre.event_id,
                   e.agent_id, pcre.created_at
            FROM purpose_capability_rule_events pcre
            JOIN event_sequence es ON es.event_id = pcre.event_id
            {latest_join}
            JOIN events e ON e.event_id = pcre.event_id
            WHERE pcre.effect IS NOT NULL
            ORDER BY pcre.purpose, pcre.capability
            ",
            latest = latest_event_cte(
                "latest",
                "purpose_capability_rule_events",
                "pcre",
                &["purpose", "capability"],
                "WHERE (?1 IS NULL OR pcre.purpose = ?1)
                   AND (?2 IS NULL OR pcre.capability = ?2)",
            ),
            latest_join =
                latest_event_join("latest", "l", "pcre", "es", &["purpose", "capability"]),
        );
        let mut stmt = conn.prepare(&sql)?;
        let rows = stmt.query_map(params![purpose, capability], purpose_capability_rule_row)?;
        collect_purpose_capability_rule_rows(rows)
    } else {
        let mut stmt = conn.prepare(
            "
            SELECT pcre.purpose, pcre.capability, pcre.effect, pcre.event_id,
                   e.agent_id, pcre.created_at
            FROM purpose_capability_rule_events pcre
            JOIN event_sequence es ON es.event_id = pcre.event_id
            JOIN events e ON e.event_id = pcre.event_id
            WHERE (?1 IS NULL OR pcre.purpose = ?1)
              AND (?2 IS NULL OR pcre.capability = ?2)
            ORDER BY es.seq, pcre.purpose, pcre.capability
            ",
        )?;
        let rows = stmt.query_map(params![purpose, capability], purpose_capability_rule_row)?;
        collect_purpose_capability_rule_rows(rows)
    }
}

pub(crate) fn set_operation_capability_rule(
    conn: &mut Connection,
    operation: &str,
    capability: &str,
    effect: Option<&str>,
    agent_id: &str,
    run_id: Option<&str>,
    tool_call_id: Option<&str>,
) -> AnfsResult<()> {
    let operation = validate_operation_capability_rule_operation(operation)?;
    let capability = validate_capability_name(capability)?;
    let effect = validate_operation_capability_rule_effect(effect)?;
    let tx = conn.transaction()?;
    let event_id = new_event_id();
    let action = if effect.is_some() { "set" } else { "clear" };
    let payload = json!({
        "operation": operation,
        "capability": capability,
        "effect": effect,
        "action": action,
    })
    .to_string();
    insert_event(
        &tx,
        &event_id,
        "operation_capability_rule",
        Some(agent_id),
        run_id,
        tool_call_id,
        None,
        Some(&payload),
    )?;
    tx.execute(
        "INSERT INTO operation_capability_rule_events
         (operation, capability, effect, event_id, created_at)
         VALUES (?1, ?2, ?3, ?4, ?5)",
        params![operation, capability, effect, event_id, now_millis()],
    )?;
    tx.commit()?;
    Ok(())
}

pub(crate) fn operation_capability_rules(
    conn: &Connection,
    operation: Option<&str>,
    capability: Option<&str>,
    active_only: bool,
) -> AnfsResult<Vec<OperationCapabilityRuleRow>> {
    if active_only {
        let sql = format!(
            "
            WITH {latest}
            SELECT ocre.operation, ocre.capability, ocre.effect, ocre.event_id,
                   e.agent_id, ocre.created_at
            FROM operation_capability_rule_events ocre
            JOIN event_sequence es ON es.event_id = ocre.event_id
            {latest_join}
            JOIN events e ON e.event_id = ocre.event_id
            WHERE ocre.effect IS NOT NULL
            ORDER BY ocre.operation, ocre.capability
            ",
            latest = latest_event_cte(
                "latest",
                "operation_capability_rule_events",
                "ocre",
                &["operation", "capability"],
                "WHERE (?1 IS NULL OR ocre.operation = ?1)
                   AND (?2 IS NULL OR ocre.capability = ?2)",
            ),
            latest_join =
                latest_event_join("latest", "l", "ocre", "es", &["operation", "capability"]),
        );
        let mut stmt = conn.prepare(&sql)?;
        let rows = stmt.query_map(
            params![operation, capability],
            operation_capability_rule_row,
        )?;
        collect_operation_capability_rule_rows(rows)
    } else {
        let mut stmt = conn.prepare(
            "
            SELECT ocre.operation, ocre.capability, ocre.effect, ocre.event_id,
                   e.agent_id, ocre.created_at
            FROM operation_capability_rule_events ocre
            JOIN event_sequence es ON es.event_id = ocre.event_id
            JOIN events e ON e.event_id = ocre.event_id
            WHERE (?1 IS NULL OR ocre.operation = ?1)
              AND (?2 IS NULL OR ocre.capability = ?2)
            ORDER BY es.seq, ocre.operation, ocre.capability
            ",
        )?;
        let rows = stmt.query_map(
            params![operation, capability],
            operation_capability_rule_row,
        )?;
        collect_operation_capability_rule_rows(rows)
    }
}

pub(crate) fn set_fragment_policy_label(
    conn: &mut Connection,
    node_id: &str,
    offset: i64,
    length: i64,
    label: &str,
    value: Option<&str>,
    agent_id: &str,
    run_id: Option<&str>,
    tool_call_id: Option<&str>,
) -> AnfsResult<()> {
    validate_fragment_policy_range(conn, node_id, offset, length)?;
    if label.trim().is_empty() {
        return Err(AnfsError::PolicyDenied(
            "fragment policy label must not be empty".to_string(),
        ));
    }
    let tx = conn.transaction()?;
    insert_fragment_policy_label_event(
        &tx,
        node_id,
        offset,
        length,
        label,
        value,
        agent_id,
        run_id,
        tool_call_id,
        None,
        None,
    )?;
    tx.commit()?;
    Ok(())
}

pub(crate) fn propagate_fragment_policy_labels_between_ranges(
    conn: &mut Connection,
    source_node_id: &str,
    source_offset: i64,
    source_length: i64,
    output_node_id: &str,
    output_offset: i64,
    output_length: i64,
    agent_id: &str,
    run_id: Option<&str>,
    tool_call_id: Option<&str>,
) -> AnfsResult<i64> {
    validate_fragment_policy_range(conn, source_node_id, source_offset, source_length)?;
    validate_fragment_policy_range(conn, output_node_id, output_offset, output_length)?;
    let source_end = source_offset.saturating_add(source_length);
    let tx = conn.transaction()?;
    let sql = format!(
        "
        WITH {latest},
        active AS (
            SELECT fple.label, fple.value
            FROM fragment_policy_label_events fple
            JOIN event_sequence es ON es.event_id = fple.event_id
            {latest_join}
            WHERE fple.value IS NOT NULL
        )
        SELECT DISTINCT label, value
        FROM active
        ORDER BY label, value
        ",
        latest = latest_event_cte(
            "latest",
            "fragment_policy_label_events",
            "fple",
            FRAGMENT_POLICY_LABEL_EVENT_KEYS,
            "WHERE fple.node_id = ?1
               AND fple.offset < ?3
               AND (fple.offset + fple.length) > ?2",
        ),
        latest_join = latest_event_join(
            "latest",
            "l",
            "fple",
            "es",
            FRAGMENT_POLICY_LABEL_EVENT_KEYS
        ),
    );
    let mut stmt = tx.prepare(&sql)?;
    let rows = stmt.query_map(params![source_node_id, source_offset, source_end], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    let mut labels = Vec::new();
    for row in rows {
        labels.push(row?);
    }
    drop(stmt);

    for (label, value) in &labels {
        insert_fragment_policy_label_event(
            &tx,
            output_node_id,
            output_offset,
            output_length,
            label,
            Some(value),
            agent_id,
            run_id,
            tool_call_id,
            Some(source_node_id),
            Some((source_offset, source_length)),
        )?;
    }
    let count = labels.len() as i64;
    tx.commit()?;
    Ok(count)
}

pub(crate) fn auto_propagate_fragment_policy_labels_by_exact_match(
    conn: &mut Connection,
    objects_dir: &Path,
    source_node_id: &str,
    output_node_id: &str,
    agent_id: &str,
    run_id: Option<&str>,
    tool_call_id: Option<&str>,
) -> AnfsResult<i64> {
    ensure_node_exists(conn, source_node_id)?;
    ensure_node_exists(conn, output_node_id)?;
    let source_bytes = read_node_bytes(conn, objects_dir, source_node_id)?;
    let output_bytes = read_node_bytes(conn, objects_dir, output_node_id)?;
    let source_labels = active_fragment_policy_label_ranges(conn, source_node_id)?;
    let tx = conn.transaction()?;
    let mut propagated = 0_i64;
    let mut seen_outputs = BTreeSet::new();
    for (source_offset, source_length, label, value) in source_labels {
        let source_start = source_offset as usize;
        let source_end = source_start.saturating_add(source_length as usize);
        if source_start >= source_bytes.len() || source_end > source_bytes.len() {
            return Err(AnfsError::StorageCorruption(format!(
                "fragment label range {source_offset}..{source_end} exceeds source node {source_node_id}"
            )));
        }
        let needle = &source_bytes[source_start..source_end];
        let Some(output_offset) = unique_subslice_offset(&output_bytes, needle) else {
            continue;
        };
        let key = (
            output_offset as i64,
            needle.len() as i64,
            label.clone(),
            value.clone(),
        );
        if !seen_outputs.insert(key.clone()) {
            continue;
        }
        insert_fragment_policy_label_event(
            &tx,
            output_node_id,
            key.0,
            key.1,
            &label,
            Some(&value),
            agent_id,
            run_id,
            tool_call_id,
            Some(source_node_id),
            Some((source_offset, source_length)),
        )?;
        propagated += 1;
    }
    tx.commit()?;
    Ok(propagated)
}

pub(crate) fn auto_propagate_fragment_policy_labels_by_normalized_scalar(
    conn: &mut Connection,
    objects_dir: &Path,
    source_node_id: &str,
    output_node_id: &str,
    agent_id: &str,
    run_id: Option<&str>,
    tool_call_id: Option<&str>,
) -> AnfsResult<i64> {
    auto_propagate_fragment_policy_labels_by_normalized_projection(
        conn,
        objects_dir,
        source_node_id,
        output_node_id,
        agent_id,
        run_id,
        tool_call_id,
        normalized_json_scalar_bytes,
    )
}

pub(crate) fn auto_propagate_fragment_policy_labels_by_normalized_json(
    conn: &mut Connection,
    objects_dir: &Path,
    source_node_id: &str,
    output_node_id: &str,
    agent_id: &str,
    run_id: Option<&str>,
    tool_call_id: Option<&str>,
) -> AnfsResult<i64> {
    auto_propagate_fragment_policy_labels_by_normalized_projection(
        conn,
        objects_dir,
        source_node_id,
        output_node_id,
        agent_id,
        run_id,
        tool_call_id,
        normalized_json_value_bytes,
    )
}

fn auto_propagate_fragment_policy_labels_by_normalized_projection(
    conn: &mut Connection,
    objects_dir: &Path,
    source_node_id: &str,
    output_node_id: &str,
    agent_id: &str,
    run_id: Option<&str>,
    tool_call_id: Option<&str>,
    normalize: fn(&[u8]) -> AnfsResult<Option<Vec<u8>>>,
) -> AnfsResult<i64> {
    ensure_node_exists(conn, source_node_id)?;
    ensure_node_exists(conn, output_node_id)?;
    let source_bytes = read_node_bytes(conn, objects_dir, source_node_id)?;
    let output_bytes = read_node_bytes(conn, objects_dir, output_node_id)?;
    let source_labels = active_fragment_policy_label_ranges(conn, source_node_id)?;
    let tx = conn.transaction()?;
    let mut propagated = 0_i64;
    let mut seen_outputs = BTreeSet::new();
    for (source_offset, source_length, label, value) in source_labels {
        let source_start = source_offset as usize;
        let source_end = source_start.saturating_add(source_length as usize);
        if source_start >= source_bytes.len() || source_end > source_bytes.len() {
            return Err(AnfsError::StorageCorruption(format!(
                "fragment label range {source_offset}..{source_end} exceeds source node {source_node_id}"
            )));
        }
        let source_fragment = &source_bytes[source_start..source_end];
        let Some(needle) = normalize(source_fragment)? else {
            continue;
        };
        let Some(output_offset) = unique_subslice_offset(&output_bytes, &needle) else {
            continue;
        };
        let key = (
            output_offset as i64,
            needle.len() as i64,
            label.clone(),
            value.clone(),
        );
        if !seen_outputs.insert(key.clone()) {
            continue;
        }
        insert_fragment_policy_label_event(
            &tx,
            output_node_id,
            key.0,
            key.1,
            &label,
            Some(&value),
            agent_id,
            run_id,
            tool_call_id,
            Some(source_node_id),
            Some((source_offset, source_length)),
        )?;
        propagated += 1;
    }
    tx.commit()?;
    Ok(propagated)
}

fn active_fragment_policy_label_ranges(
    conn: &Connection,
    node_id: &str,
) -> AnfsResult<Vec<(i64, i64, String, String)>> {
    let sql = format!(
        "
        WITH {latest}
        SELECT fple.offset, fple.length, fple.label, fple.value
        FROM fragment_policy_label_events fple
        JOIN event_sequence es ON es.event_id = fple.event_id
        {latest_join}
        WHERE fple.value IS NOT NULL
        ORDER BY fple.offset, fple.length, fple.label
        ",
        latest = latest_event_cte(
            "latest",
            "fragment_policy_label_events",
            "fple",
            FRAGMENT_POLICY_LABEL_EVENT_KEYS,
            "WHERE fple.node_id = ?1",
        ),
        latest_join = latest_event_join(
            "latest",
            "l",
            "fple",
            "es",
            FRAGMENT_POLICY_LABEL_EVENT_KEYS
        ),
    );
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(params![node_id], |row| {
        Ok((
            row.get::<_, i64>(0)?,
            row.get::<_, i64>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, String>(3)?,
        ))
    })?;
    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}

fn unique_subslice_offset(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() || needle.len() > haystack.len() {
        return None;
    }
    let mut found = None;
    for offset in 0..=haystack.len() - needle.len() {
        if &haystack[offset..offset + needle.len()] == needle {
            if found.is_some() {
                return None;
            }
            found = Some(offset);
        }
    }
    found
}

fn normalized_json_scalar_bytes(source_fragment: &[u8]) -> AnfsResult<Option<Vec<u8>>> {
    let trimmed = trim_ascii_bytes(source_fragment);
    if trimmed.is_empty() {
        return Ok(None);
    }

    let value = serde_json::from_slice::<Value>(trimmed).map_err(|err| {
        AnfsError::StorageCorruption(format!(
            "labeled source fragment is not valid JSON scalar: {err}"
        ))
    })?;
    match value {
        Value::Array(_) | Value::Object(_) => Ok(None),
        value => normalized_json_scalar_value_bytes(value),
    }
}

fn normalized_json_value_bytes(source_fragment: &[u8]) -> AnfsResult<Option<Vec<u8>>> {
    let trimmed = trim_ascii_bytes(source_fragment);
    if trimmed.is_empty() {
        return Ok(None);
    }
    let value = serde_json::from_slice::<Value>(trimmed).map_err(|err| {
        AnfsError::StorageCorruption(format!(
            "labeled source fragment is not valid JSON value: {err}"
        ))
    })?;
    match value {
        Value::Array(_) | Value::Object(_) => serde_json::to_vec(&value).map(Some).map_err(|err| {
            AnfsError::StorageCorruption(format!(
                "failed to serialize normalized JSON value: {err}"
            ))
        }),
        value => normalized_json_scalar_value_bytes(value),
    }
}

fn normalized_json_scalar_value_bytes(value: Value) -> AnfsResult<Option<Vec<u8>>> {
    match value {
        Value::String(value) => {
            let bytes = value.into_bytes();
            if bytes.is_empty() {
                Ok(None)
            } else {
                Ok(Some(bytes))
            }
        }
        Value::Number(number) => {
            if let Some(value) = number.as_i64() {
                return Ok(Some(value.to_string().into_bytes()));
            }
            if let Some(value) = number.as_u64() {
                return Ok(Some(value.to_string().into_bytes()));
            }
            if let Some(value) = number.as_f64() {
                if value.is_finite() {
                    return Ok(Some(value.to_string().into_bytes()));
                }
            }
            Ok(None)
        }
        Value::Bool(value) => Ok(Some(if value {
            b"true".to_vec()
        } else {
            b"false".to_vec()
        })),
        Value::Null => Ok(Some(b"null".to_vec())),
        _ => Ok(None),
    }
}

fn trim_ascii_bytes(bytes: &[u8]) -> &[u8] {
    let start = bytes
        .iter()
        .take_while(|byte| byte.is_ascii_whitespace())
        .count();
    let mut end = bytes.len();
    while end > start && bytes[end - 1].is_ascii_whitespace() {
        end -= 1;
    }
    &bytes[start..end]
}

pub(crate) fn propagate_fragment_policy_labels_for_blob(
    tx: &Transaction<'_>,
    node_id: &str,
    blob_hash: &str,
    agent_id: &str,
    run_id: Option<&str>,
    tool_call_id: Option<&str>,
) -> AnfsResult<()> {
    let sql = format!(
        "
        WITH {latest},
        active AS (
            SELECT fple.node_id, fple.offset, fple.length, fple.label, fple.value
            FROM fragment_policy_label_events fple
            JOIN event_sequence es ON es.event_id = fple.event_id
            {latest_join}
            WHERE fple.value IS NOT NULL
        ),
        ranked AS (
            SELECT node_id, offset, length, label, value,
                   ROW_NUMBER() OVER (
                       PARTITION BY offset, length, label, value
                       ORDER BY node_id
                   ) AS rn
            FROM active
        )
        SELECT node_id, offset, length, label, value
        FROM ranked
        WHERE rn = 1
        ORDER BY offset, length, label, value
        ",
        latest = latest_event_cte(
            "latest",
            "fragment_policy_label_events",
            "fple",
            FRAGMENT_POLICY_LABEL_EVENT_KEYS,
            "JOIN nodes n ON n.node_id = fple.node_id
             WHERE n.blob_hash = ?1
               AND fple.node_id <> ?2",
        ),
        latest_join = latest_event_join(
            "latest",
            "l",
            "fple",
            "es",
            FRAGMENT_POLICY_LABEL_EVENT_KEYS
        ),
    );
    let mut stmt = tx.prepare(&sql)?;
    let rows = stmt.query_map(params![blob_hash, node_id], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, i64>(1)?,
            row.get::<_, i64>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, String>(4)?,
        ))
    })?;
    let mut labels = Vec::new();
    for row in rows {
        labels.push(row?);
    }
    drop(stmt);

    for (source_node_id, offset, length, label, value) in labels {
        insert_fragment_policy_label_event(
            tx,
            node_id,
            offset,
            length,
            &label,
            Some(&value),
            agent_id,
            run_id,
            tool_call_id,
            Some(&source_node_id),
            None,
        )?;
    }
    Ok(())
}

pub(crate) fn propagate_derived_policy_labels(
    tx: &Transaction<'_>,
    node_id: &str,
    blob_hash: &str,
    blob_size: i64,
    derived_from_nodes: &[String],
    agent_id: &str,
    run_id: Option<&str>,
    tool_call_id: Option<&str>,
) -> AnfsResult<()> {
    if derived_from_nodes.is_empty() {
        return Ok(());
    }

    let mut node_labels = Vec::new();
    let mut fragment_labels = Vec::new();
    for source_node_id in derived_from_nodes {
        collect_active_node_policy_labels(tx, source_node_id, &mut node_labels)?;
        collect_active_fragment_policy_labels_for_derived_output(
            tx,
            source_node_id,
            blob_hash,
            &mut fragment_labels,
        )?;
    }
    node_labels.sort();
    node_labels.dedup();
    fragment_labels.sort();
    fragment_labels.dedup();

    for (source_node_id, label, value) in node_labels {
        insert_policy_label_event(
            tx,
            "node",
            node_id,
            node_id,
            &label,
            Some(&value),
            agent_id,
            run_id,
            tool_call_id,
            Some(&source_node_id),
        )?;
    }

    if blob_size > 0 {
        for (source_node_id, label, value) in fragment_labels {
            insert_fragment_policy_label_event(
                tx,
                node_id,
                0,
                blob_size,
                &label,
                Some(&value),
                agent_id,
                run_id,
                tool_call_id,
                Some(&source_node_id),
                None,
            )?;
        }
    }
    Ok(())
}

fn collect_active_node_policy_labels(
    tx: &Transaction<'_>,
    source_node_id: &str,
    out: &mut Vec<(String, String, String)>,
) -> AnfsResult<()> {
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
            "WHERE ple.subject_type = 'node'
               AND ple.subject_id = ?1
               AND ple.value IS NOT NULL",
        ),
        latest_join = latest_event_join("latest_value", "l", "ple", "es", POLICY_LABEL_EVENT_KEYS),
        clear_guard = policy_label_clear_guard("ple", "es"),
    );
    let mut stmt = tx.prepare(&sql)?;
    let rows = stmt.query_map(params![source_node_id], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    for row in rows {
        let (label, value) = row?;
        out.push((source_node_id.to_string(), label, value));
    }
    Ok(())
}

fn collect_active_fragment_policy_labels_for_derived_output(
    tx: &Transaction<'_>,
    source_node_id: &str,
    new_blob_hash: &str,
    out: &mut Vec<(String, String, String)>,
) -> AnfsResult<()> {
    let source_blob_hash: Option<String> = tx
        .query_row(
            "SELECT blob_hash FROM nodes WHERE node_id = ?1",
            params![source_node_id],
            |row| row.get(0),
        )
        .optional()?;
    if source_blob_hash.as_deref() == Some(new_blob_hash) {
        return Ok(());
    }

    let sql = format!(
        "
        WITH {latest}
        SELECT fple.label, fple.value
        FROM fragment_policy_label_events fple
        JOIN event_sequence es ON es.event_id = fple.event_id
        {latest_join}
        WHERE fple.value IS NOT NULL
        ORDER BY fple.label, fple.value
        ",
        latest = latest_event_cte(
            "latest",
            "fragment_policy_label_events",
            "fple",
            FRAGMENT_POLICY_LABEL_EVENT_KEYS,
            "WHERE fple.node_id = ?1",
        ),
        latest_join = latest_event_join(
            "latest",
            "l",
            "fple",
            "es",
            FRAGMENT_POLICY_LABEL_EVENT_KEYS
        ),
    );
    let mut stmt = tx.prepare(&sql)?;
    let rows = stmt.query_map(params![source_node_id], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    for row in rows {
        let (label, value) = row?;
        out.push((source_node_id.to_string(), label, value));
    }
    Ok(())
}

fn insert_fragment_policy_label_event(
    tx: &Transaction<'_>,
    node_id: &str,
    offset: i64,
    length: i64,
    label: &str,
    value: Option<&str>,
    agent_id: &str,
    run_id: Option<&str>,
    tool_call_id: Option<&str>,
    propagated_from_node_id: Option<&str>,
    propagated_from_range: Option<(i64, i64)>,
) -> AnfsResult<()> {
    let event_id = new_event_id();
    let action = if value.is_some() { "set" } else { "clear" };
    let mut payload = json!({
        "node_id": node_id,
        "offset": offset,
        "length": length,
        "label": label,
        "value": value,
        "action": action,
    });
    if let Some(source_node_id) = propagated_from_node_id {
        payload["propagated_from_node_id"] = json!(source_node_id);
    }
    if let Some((offset, length)) = propagated_from_range {
        payload["propagated_from_offset"] = json!(offset);
        payload["propagated_from_length"] = json!(length);
    }
    let payload = payload.to_string();
    insert_event(
        tx,
        &event_id,
        "fragment_policy_label",
        Some(agent_id),
        run_id,
        tool_call_id,
        None,
        Some(&payload),
    )?;
    insert_edge(
        tx,
        &event_id,
        "input",
        node_id,
        "fragment_policy_subject",
        Some(node_id),
    )?;
    tx.execute(
        "INSERT INTO fragment_policy_label_events
         (node_id, offset, length, label, value, event_id, created_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        params![
            node_id,
            offset,
            length,
            label,
            value,
            event_id,
            now_millis()
        ],
    )?;
    Ok(())
}

pub(crate) fn fragment_policy_labels(
    conn: &Connection,
    node_id: Option<&str>,
    label: Option<&str>,
    active_only: bool,
) -> AnfsResult<Vec<FragmentPolicyLabelRow>> {
    if active_only {
        let sql = format!(
            "
            WITH {latest}
            SELECT fple.node_id, fple.offset, fple.length, fple.label, fple.value,
                   fple.event_id, e.agent_id, fple.created_at
            FROM fragment_policy_label_events fple
            JOIN event_sequence es ON es.event_id = fple.event_id
            {latest_join}
            JOIN events e ON e.event_id = fple.event_id
            WHERE fple.value IS NOT NULL
            ORDER BY fple.node_id, fple.offset, fple.length, fple.label
            ",
            latest = latest_event_cte(
                "latest",
                "fragment_policy_label_events",
                "fple",
                FRAGMENT_POLICY_LABEL_EVENT_KEYS,
                "WHERE (?1 IS NULL OR fple.node_id = ?1)
                   AND (?2 IS NULL OR fple.label = ?2)",
            ),
            latest_join = latest_event_join(
                "latest",
                "l",
                "fple",
                "es",
                FRAGMENT_POLICY_LABEL_EVENT_KEYS
            ),
        );
        let mut stmt = conn.prepare(&sql)?;
        let rows = stmt.query_map(params![node_id, label], fragment_policy_label_row)?;
        collect_fragment_policy_label_rows(rows)
    } else {
        let mut stmt = conn.prepare(
            "
            SELECT fple.node_id, fple.offset, fple.length, fple.label, fple.value,
                   fple.event_id, e.agent_id, fple.created_at
            FROM fragment_policy_label_events fple
            JOIN event_sequence es ON es.event_id = fple.event_id
            JOIN events e ON e.event_id = fple.event_id
            WHERE (?1 IS NULL OR fple.node_id = ?1)
              AND (?2 IS NULL OR fple.label = ?2)
            ORDER BY es.seq, fple.node_id, fple.offset, fple.length, fple.label
            ",
        )?;
        let rows = stmt.query_map(params![node_id, label], fragment_policy_label_row)?;
        collect_fragment_policy_label_rows(rows)
    }
}

pub(crate) fn active_fragment_policy_blocks_range(
    conn: &Connection,
    node_id: &str,
    offset: i64,
    length: i64,
) -> AnfsResult<bool> {
    if length <= 0 {
        return Ok(false);
    }
    let end = offset.saturating_add(length);
    let sql = format!(
        "
        WITH {latest}
        SELECT fple.label, fple.value
        FROM fragment_policy_label_events fple
        JOIN event_sequence es ON es.event_id = fple.event_id
        {latest_join}
        WHERE fple.value IS NOT NULL
        ORDER BY fple.offset, fple.label
        ",
        latest = latest_event_cte(
            "latest",
            "fragment_policy_label_events",
            "fple",
            FRAGMENT_POLICY_LABEL_EVENT_KEYS,
            "WHERE fple.node_id = ?1
               AND fple.offset < ?3
               AND (fple.offset + fple.length) > ?2",
        ),
        latest_join = latest_event_join(
            "latest",
            "l",
            "fple",
            "es",
            FRAGMENT_POLICY_LABEL_EVENT_KEYS
        ),
    );
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(params![node_id, offset, end], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    let mut labels = Vec::new();
    for row in rows {
        let (label, value) = row?;
        if active_policy_rule_denies_label_value(conn, "fragment", &label, &value)?
            || active_policy_rule_denies_label_value(conn, "*", &label, &value)?
        {
            return Ok(true);
        }
        labels.push((label, value));
    }
    active_policy_expression_rules_block_labels(conn, "fragment", &labels)
}

pub(crate) fn active_fragment_policy_blocks_node(
    conn: &Connection,
    node_id: &str,
) -> AnfsResult<bool> {
    let size: Option<i64> = conn
        .query_row(
            "SELECT b.size FROM nodes n JOIN blobs b ON b.hash = n.blob_hash WHERE n.node_id = ?1",
            params![node_id],
            |row| row.get(0),
        )
        .optional()?;
    let Some(size) = size else {
        return Ok(false);
    };
    active_fragment_policy_blocks_range(conn, node_id, 0, size)
}

pub(crate) fn active_policy_rule_denies_label_value(
    conn: &Connection,
    subject_type: &str,
    label: &str,
    value: &str,
) -> AnfsResult<bool> {
    let sql = format!(
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
            "WHERE pre.scope = 'visibility'
               AND pre.label = ?1
               AND pre.subject_type IN ('*', ?2)
               AND pre.value IN ('*', ?3)",
        ),
        latest_join = latest_event_join("latest", "l", "pre", "es", POLICY_RULE_EVENT_KEYS),
    );
    let count: i64 = conn.query_row(&sql, params![label, subject_type, value], |row| row.get(0))?;
    Ok(count > 0)
}

pub(crate) fn active_policy_expression_rules_block_subject(
    conn: &Connection,
    subject_type: &str,
    subject_id: &str,
) -> AnfsResult<bool> {
    let labels = active_subject_policy_label_values(conn, subject_type, subject_id)?;
    active_policy_expression_rules_block_labels(conn, subject_type, &labels)
}

pub(crate) fn active_policy_expression_rules_block_labels(
    conn: &Connection,
    subject_type: &str,
    labels: &[(String, String)],
) -> AnfsResult<bool> {
    let sql = format!(
        "
        WITH {latest}
        SELECT pere.expression_json
        FROM policy_expression_rule_events pere
        JOIN event_sequence es ON es.event_id = pere.event_id
        {latest_join}
        WHERE pere.effect = 'deny'
        ORDER BY pere.subject_type, pere.expression_json
        ",
        latest = latest_event_cte(
            "latest",
            "policy_expression_rule_events",
            "pere",
            POLICY_EXPRESSION_RULE_EVENT_KEYS,
            "WHERE pere.scope = 'visibility'
               AND pere.subject_type IN ('*', ?1)",
        ),
        latest_join = latest_event_join(
            "latest",
            "l",
            "pere",
            "es",
            POLICY_EXPRESSION_RULE_EVENT_KEYS
        ),
    );
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(params![subject_type], |row| row.get::<_, String>(0))?;
    for row in rows {
        let expression_json = row?;
        let expression = parse_policy_expression(&expression_json)?;
        if eval_policy_expression(&expression, labels)? {
            return Ok(true);
        }
    }
    Ok(false)
}

pub(crate) fn active_purpose_policy_blocks_ref_node(
    conn: &Connection,
    purpose: &str,
    ref_name: &str,
    node_id: &str,
) -> AnfsResult<bool> {
    let purpose = validate_purpose_policy_rule_purpose(purpose)?;
    Ok(
        active_purpose_policy_rules_block_subject(conn, purpose, "ref", ref_name)?
            || active_purpose_policy_rules_block_subject(conn, purpose, "node", node_id)?
            || active_purpose_policy_rules_block_node_fragments(conn, purpose, node_id)?,
    )
}

pub(crate) fn active_purpose_policy_blocks_ref_node_range(
    conn: &Connection,
    purpose: &str,
    ref_name: &str,
    node_id: &str,
    offset: i64,
    length: i64,
) -> AnfsResult<bool> {
    let purpose = validate_purpose_policy_rule_purpose(purpose)?;
    Ok(
        active_purpose_policy_rules_block_subject(conn, purpose, "ref", ref_name)?
            || active_purpose_policy_rules_block_subject(conn, purpose, "node", node_id)?
            || active_purpose_policy_rules_block_node_fragment_range(
                conn, purpose, node_id, offset, length,
            )?,
    )
}

pub(crate) fn active_purpose_capability_missing(
    conn: &Connection,
    purpose: &str,
    agent_id: &str,
) -> AnfsResult<Option<String>> {
    let purpose = validate_purpose_policy_rule_purpose(purpose)?;
    let agent_id = validate_capability_agent_id(agent_id)?;
    let sql = format!(
        "
        WITH {latest}
        SELECT pcre.capability
        FROM purpose_capability_rule_events pcre
        JOIN event_sequence es ON es.event_id = pcre.event_id
        {latest_join}
        WHERE pcre.effect = 'require'
        ORDER BY pcre.capability
        ",
        latest = latest_event_cte(
            "latest_rules",
            "purpose_capability_rule_events",
            "pcre",
            &["purpose", "capability"],
            "WHERE pcre.purpose = ?1",
        ),
        latest_join = latest_event_join(
            "latest_rules",
            "l",
            "pcre",
            "es",
            &["purpose", "capability"]
        ),
    );
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(params![purpose], |row| row.get::<_, String>(0))?;
    for row in rows {
        let capability = row?;
        if !agent_has_active_capability(conn, agent_id, &capability)? {
            return Ok(Some(capability));
        }
    }
    Ok(None)
}

pub(crate) fn active_operation_capability_missing(
    conn: &Connection,
    operation: &str,
    agent_id: &str,
) -> AnfsResult<Option<String>> {
    let operation = validate_operation_capability_rule_operation(operation)?;
    let agent_id = validate_capability_agent_id(agent_id)?;
    let sql = format!(
        "
        WITH {latest}
        SELECT ocre.capability
        FROM operation_capability_rule_events ocre
        JOIN event_sequence es ON es.event_id = ocre.event_id
        {latest_join}
        WHERE ocre.effect = 'require'
        ORDER BY ocre.capability
        ",
        latest = latest_event_cte(
            "latest_rules",
            "operation_capability_rule_events",
            "ocre",
            &["operation", "capability"],
            "WHERE ocre.operation = ?1",
        ),
        latest_join = latest_event_join(
            "latest_rules",
            "l",
            "ocre",
            "es",
            &["operation", "capability"]
        ),
    );
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(params![operation], |row| row.get::<_, String>(0))?;
    for row in rows {
        let capability = row?;
        if !agent_has_active_capability(conn, agent_id, &capability)? {
            return Ok(Some(capability));
        }
    }
    Ok(None)
}

fn agent_has_active_capability(
    conn: &Connection,
    agent_id: &str,
    capability: &str,
) -> AnfsResult<bool> {
    let sql = format!(
        "
        WITH {latest}
        SELECT COUNT(*)
        FROM agent_capability_events ace
        JOIN event_sequence es ON es.event_id = ace.event_id
        {latest_join}
        WHERE ace.effect = 'grant'
        ",
        latest = latest_event_cte(
            "latest",
            "agent_capability_events",
            "ace",
            &["agent_id", "capability"],
            "WHERE ace.agent_id = ?1
               AND ace.capability = ?2",
        ),
        latest_join = latest_event_join("latest", "l", "ace", "es", &["agent_id", "capability"]),
    );
    let count: i64 = conn.query_row(&sql, params![agent_id, capability], |row| row.get(0))?;
    Ok(count > 0)
}

fn active_purpose_policy_rules_block_subject(
    conn: &Connection,
    purpose: &str,
    subject_type: &str,
    subject_id: &str,
) -> AnfsResult<bool> {
    let labels = active_subject_policy_label_values(conn, subject_type, subject_id)?;
    for (label, value) in labels {
        if active_purpose_policy_rule_denies_label_value(
            conn,
            purpose,
            subject_type,
            &label,
            &value,
        )? {
            return Ok(true);
        }
    }
    Ok(false)
}

fn active_purpose_policy_rules_block_node_fragments(
    conn: &Connection,
    purpose: &str,
    node_id: &str,
) -> AnfsResult<bool> {
    let sql = format!(
        "
        WITH {latest}
        SELECT fple.label, fple.value
        FROM fragment_policy_label_events fple
        JOIN event_sequence es ON es.event_id = fple.event_id
        {latest_join}
        WHERE fple.value IS NOT NULL
        ORDER BY fple.offset, fple.label
        ",
        latest = latest_event_cte(
            "latest",
            "fragment_policy_label_events",
            "fple",
            FRAGMENT_POLICY_LABEL_EVENT_KEYS,
            "WHERE fple.node_id = ?1",
        ),
        latest_join = latest_event_join(
            "latest",
            "l",
            "fple",
            "es",
            FRAGMENT_POLICY_LABEL_EVENT_KEYS
        ),
    );
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(params![node_id], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    for row in rows {
        let (label, value) = row?;
        if active_purpose_policy_rule_denies_label_value(conn, purpose, "fragment", &label, &value)?
        {
            return Ok(true);
        }
    }
    Ok(false)
}

fn active_purpose_policy_rules_block_node_fragment_range(
    conn: &Connection,
    purpose: &str,
    node_id: &str,
    offset: i64,
    length: i64,
) -> AnfsResult<bool> {
    if offset < 0 {
        return Err(AnfsError::PolicyDenied(
            "range offset must be non-negative".to_string(),
        ));
    }
    if length <= 0 {
        return Err(AnfsError::PolicyDenied(
            "range length must be positive".to_string(),
        ));
    }
    let end = offset
        .checked_add(length)
        .ok_or_else(|| AnfsError::PolicyDenied("range offset plus length overflows".to_string()))?;
    let sql = format!(
        "
        WITH {latest}
        SELECT fple.label, fple.value
        FROM fragment_policy_label_events fple
        JOIN event_sequence es ON es.event_id = fple.event_id
        {latest_join}
        WHERE fple.value IS NOT NULL
        ORDER BY fple.offset, fple.label
        ",
        latest = latest_event_cte(
            "latest",
            "fragment_policy_label_events",
            "fple",
            FRAGMENT_POLICY_LABEL_EVENT_KEYS,
            "WHERE fple.node_id = ?1
               AND fple.offset < ?3
               AND fple.offset + fple.length > ?2",
        ),
        latest_join = latest_event_join(
            "latest",
            "l",
            "fple",
            "es",
            FRAGMENT_POLICY_LABEL_EVENT_KEYS
        ),
    );
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(params![node_id, offset, end], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    for row in rows {
        let (label, value) = row?;
        if active_purpose_policy_rule_denies_label_value(conn, purpose, "fragment", &label, &value)?
        {
            return Ok(true);
        }
    }
    Ok(false)
}

fn active_purpose_policy_rule_denies_label_value(
    conn: &Connection,
    purpose: &str,
    subject_type: &str,
    label: &str,
    value: &str,
) -> AnfsResult<bool> {
    let sql = format!(
        "
        WITH {latest}
        SELECT COUNT(*)
        FROM purpose_policy_rule_events ppre
        JOIN event_sequence es ON es.event_id = ppre.event_id
        {latest_join}
        WHERE ppre.effect = 'deny'
        ",
        latest = latest_event_cte(
            "latest",
            "purpose_policy_rule_events",
            "ppre",
            &["purpose", "subject_type", "label", "value"],
            "WHERE ppre.purpose = ?1
               AND ppre.label = ?3
               AND ppre.subject_type IN ('*', ?2)
               AND ppre.value IN ('*', ?4)",
        ),
        latest_join = latest_event_join(
            "latest",
            "l",
            "ppre",
            "es",
            &["purpose", "subject_type", "label", "value"],
        ),
    );
    let count: i64 = conn.query_row(&sql, params![purpose, subject_type, label, value], |row| {
        row.get(0)
    })?;
    Ok(count > 0)
}

pub(crate) fn resolve_policy_label_subject(
    conn: &Connection,
    subject_type: &str,
    subject_id: &str,
) -> AnfsResult<String> {
    match subject_type {
        "ref" => {
            let rec = require_ref(conn, subject_id)?;
            Ok(rec.node_id)
        }
        "node" => {
            ensure_node_exists(conn, subject_id)?;
            Ok(subject_id.to_string())
        }
        other => Err(AnfsError::PolicyDenied(format!(
            "unsupported policy label subject_type {other}"
        ))),
    }
}

fn active_subject_policy_label_values(
    conn: &Connection,
    subject_type: &str,
    subject_id: &str,
) -> AnfsResult<Vec<(String, String)>> {
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
    let mut labels = Vec::new();
    for row in rows {
        labels.push(row?);
    }
    Ok(labels)
}

fn validate_policy_rule_label(label: &str) -> AnfsResult<&str> {
    let label = label.trim();
    if label.is_empty() {
        return Err(AnfsError::PolicyDenied(
            "policy rule label must not be empty".to_string(),
        ));
    }
    Ok(label)
}

fn validate_policy_rule_value(value: Option<&str>) -> AnfsResult<&str> {
    match value {
        Some(value) => {
            let value = value.trim();
            if value.is_empty() || value == "*" {
                return Err(AnfsError::PolicyDenied(
                    "policy rule value must be non-empty and must not be '*'".to_string(),
                ));
            }
            Ok(value)
        }
        None => Ok("*"),
    }
}

fn validate_policy_rule_effect(effect: Option<&str>) -> AnfsResult<Option<&str>> {
    match effect {
        Some("deny") => Ok(Some("deny")),
        Some(other) => Err(AnfsError::PolicyDenied(format!(
            "unsupported policy rule effect {other}"
        ))),
        None => Ok(None),
    }
}

fn validate_policy_rule_scope(scope: &str) -> AnfsResult<&str> {
    match scope {
        "visibility" => Ok(scope),
        other => Err(AnfsError::PolicyDenied(format!(
            "unsupported policy rule scope {other}"
        ))),
    }
}

fn validate_policy_rule_subject_type(subject_type: &str) -> AnfsResult<&str> {
    match subject_type {
        "*" | "ref" | "node" | "fragment" => Ok(subject_type),
        other => Err(AnfsError::PolicyDenied(format!(
            "unsupported policy rule subject_type {other}"
        ))),
    }
}

fn validate_purpose_policy_rule_purpose(purpose: &str) -> AnfsResult<&str> {
    let purpose = purpose.trim();
    if purpose.is_empty() {
        return Err(AnfsError::PolicyDenied(
            "purpose policy rule purpose must not be empty".to_string(),
        ));
    }
    Ok(purpose)
}

fn validate_purpose_policy_rule_subject_type(subject_type: &str) -> AnfsResult<&str> {
    match subject_type {
        "*" | "ref" | "node" | "fragment" => Ok(subject_type),
        other => Err(AnfsError::PolicyDenied(format!(
            "unsupported purpose policy rule subject_type {other}"
        ))),
    }
}

fn validate_capability_agent_id(agent_id: &str) -> AnfsResult<&str> {
    let agent_id = agent_id.trim();
    if agent_id.is_empty() {
        return Err(AnfsError::PolicyDenied(
            "capability agent_id must not be empty".to_string(),
        ));
    }
    Ok(agent_id)
}

fn validate_capability_name(capability: &str) -> AnfsResult<&str> {
    let capability = capability.trim();
    if capability.is_empty() {
        return Err(AnfsError::PolicyDenied(
            "capability must not be empty".to_string(),
        ));
    }
    Ok(capability)
}

fn validate_agent_capability_effect(effect: Option<&str>) -> AnfsResult<Option<&str>> {
    match effect {
        Some("grant") => Ok(effect),
        Some(other) => Err(AnfsError::PolicyDenied(format!(
            "unsupported agent capability effect {other}"
        ))),
        None => Ok(None),
    }
}

fn validate_purpose_capability_rule_effect(effect: Option<&str>) -> AnfsResult<Option<&str>> {
    match effect {
        Some("require") => Ok(effect),
        Some(other) => Err(AnfsError::PolicyDenied(format!(
            "unsupported purpose capability rule effect {other}"
        ))),
        None => Ok(None),
    }
}

fn validate_operation_capability_rule_operation(operation: &str) -> AnfsResult<&str> {
    let operation = operation.trim();
    if operation.is_empty() {
        return Err(AnfsError::PolicyDenied(
            "operation capability rule operation must not be empty".to_string(),
        ));
    }
    if !operation
        .bytes()
        .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.'))
    {
        return Err(AnfsError::PolicyDenied(format!(
            "unsupported operation capability rule operation {operation}"
        )));
    }
    Ok(operation)
}

fn validate_operation_capability_rule_effect(effect: Option<&str>) -> AnfsResult<Option<&str>> {
    match effect {
        Some("require") => Ok(effect),
        Some(other) => Err(AnfsError::PolicyDenied(format!(
            "unsupported operation capability rule effect {other}"
        ))),
        None => Ok(None),
    }
}

fn validate_policy_expression_json(expression_json: &str) -> AnfsResult<&str> {
    let expression_json = expression_json.trim();
    if expression_json.is_empty() {
        return Err(AnfsError::PolicyDenied(
            "policy expression JSON must not be empty".to_string(),
        ));
    }
    parse_policy_expression(expression_json)?;
    Ok(expression_json)
}

fn parse_policy_expression(expression_json: &str) -> AnfsResult<Value> {
    let expression: Value = serde_json::from_str(expression_json)
        .map_err(|err| AnfsError::PolicyDenied(format!("invalid policy expression JSON: {err}")))?;
    validate_policy_expression(&expression)?;
    Ok(expression)
}

fn validate_policy_expression(expression: &Value) -> AnfsResult<()> {
    let Some(object) = expression.as_object() else {
        return Err(AnfsError::PolicyDenied(
            "policy expression must be a JSON object".to_string(),
        ));
    };
    let operator_count = ["label", "all", "any", "not", "at_least"]
        .iter()
        .filter(|key| object.contains_key(**key))
        .count();
    if operator_count != 1 {
        return Err(AnfsError::PolicyDenied(
            "policy expression must contain exactly one of label, all, any, not, or at_least"
                .to_string(),
        ));
    }
    if let Some(label) = object.get("label") {
        let Some(label) = label.as_str() else {
            return Err(AnfsError::PolicyDenied(
                "policy expression label must be a string".to_string(),
            ));
        };
        validate_policy_rule_label(label)?;
        if let Some(value) = object.get("value") {
            let Some(value) = value.as_str() else {
                return Err(AnfsError::PolicyDenied(
                    "policy expression value must be a string".to_string(),
                ));
            };
            validate_policy_rule_value(Some(value))?;
        }
        return Ok(());
    }
    for key in ["all", "any"] {
        if let Some(children) = object.get(key) {
            let Some(children) = children.as_array() else {
                return Err(AnfsError::PolicyDenied(format!(
                    "policy expression {key} must be an array"
                )));
            };
            if children.is_empty() {
                return Err(AnfsError::PolicyDenied(format!(
                    "policy expression {key} must not be empty"
                )));
            }
            for child in children {
                validate_policy_expression(child)?;
            }
            return Ok(());
        }
    }
    if let Some(child) = object.get("not") {
        validate_policy_expression(child)?;
        return Ok(());
    }
    if let Some(at_least) = object.get("at_least") {
        let Some(at_least) = at_least.as_object() else {
            return Err(AnfsError::PolicyDenied(
                "policy expression at_least must be an object".to_string(),
            ));
        };
        let count = at_least
            .get("count")
            .and_then(Value::as_i64)
            .ok_or_else(|| {
                AnfsError::PolicyDenied(
                    "policy expression at_least.count must be an integer".to_string(),
                )
            })?;
        let children = at_least
            .get("of")
            .and_then(Value::as_array)
            .ok_or_else(|| {
                AnfsError::PolicyDenied(
                    "policy expression at_least.of must be an array".to_string(),
                )
            })?;
        if children.is_empty() {
            return Err(AnfsError::PolicyDenied(
                "policy expression at_least.of must not be empty".to_string(),
            ));
        }
        if count < 1 || count as usize > children.len() {
            return Err(AnfsError::PolicyDenied(format!(
                "policy expression at_least.count must be between 1 and {}",
                children.len()
            )));
        }
        for child in children {
            validate_policy_expression(child)?;
        }
        return Ok(());
    }
    Ok(())
}

fn eval_policy_expression(expression: &Value, labels: &[(String, String)]) -> AnfsResult<bool> {
    let object = expression.as_object().ok_or_else(|| {
        AnfsError::StorageCorruption("stored policy expression is not an object".to_string())
    })?;
    if let Some(label) = object.get("label") {
        let label = label.as_str().ok_or_else(|| {
            AnfsError::StorageCorruption("stored policy expression label is invalid".to_string())
        })?;
        let value = object.get("value").and_then(|value| value.as_str());
        return Ok(labels.iter().any(|(active_label, active_value)| {
            active_label == label && value.is_none_or(|value| active_value == value)
        }));
    }
    if let Some(children) = object.get("all").and_then(|value| value.as_array()) {
        for child in children {
            if !eval_policy_expression(child, labels)? {
                return Ok(false);
            }
        }
        return Ok(true);
    }
    if let Some(children) = object.get("any").and_then(|value| value.as_array()) {
        for child in children {
            if eval_policy_expression(child, labels)? {
                return Ok(true);
            }
        }
        return Ok(false);
    }
    if let Some(child) = object.get("not") {
        return Ok(!eval_policy_expression(child, labels)?);
    }
    if let Some(at_least) = object.get("at_least").and_then(Value::as_object) {
        let count = at_least
            .get("count")
            .and_then(Value::as_i64)
            .ok_or_else(|| {
                AnfsError::StorageCorruption(
                    "stored policy expression at_least.count is invalid".to_string(),
                )
            })?;
        let children = at_least
            .get("of")
            .and_then(Value::as_array)
            .ok_or_else(|| {
                AnfsError::StorageCorruption(
                    "stored policy expression at_least.of is invalid".to_string(),
                )
            })?;
        let mut matches = 0_i64;
        for child in children {
            if eval_policy_expression(child, labels)? {
                matches += 1;
                if matches >= count {
                    return Ok(true);
                }
            }
        }
        return Ok(false);
    }
    Err(AnfsError::StorageCorruption(
        "stored policy expression has no operator".to_string(),
    ))
}

fn validate_fragment_policy_range(
    conn: &Connection,
    node_id: &str,
    offset: i64,
    length: i64,
) -> AnfsResult<()> {
    if offset < 0 {
        return Err(AnfsError::PolicyDenied(
            "fragment policy offset must be non-negative".to_string(),
        ));
    }
    if length <= 0 {
        return Err(AnfsError::PolicyDenied(
            "fragment policy length must be positive".to_string(),
        ));
    }
    let size: i64 = conn
        .query_row(
            "SELECT b.size FROM nodes n JOIN blobs b ON b.hash = n.blob_hash WHERE n.node_id = ?1",
            params![node_id],
            |row| row.get(0),
        )
        .map_err(|err| match err {
            rusqlite::Error::QueryReturnedNoRows => AnfsError::NodeNotFound(node_id.to_string()),
            other => AnfsError::Sqlite(other),
        })?;
    let end = offset
        .checked_add(length)
        .ok_or_else(|| AnfsError::PolicyDenied("fragment policy range overflow".to_string()))?;
    if end > size {
        return Err(AnfsError::PolicyDenied(format!(
            "fragment policy range {offset}..{end} exceeds node size {size}"
        )));
    }
    Ok(())
}

fn policy_label_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<PolicyLabelRow> {
    Ok((
        row.get::<_, String>(0)?,
        row.get::<_, String>(1)?,
        row.get::<_, String>(2)?,
        row.get::<_, Option<String>>(3)?,
        row.get::<_, String>(4)?,
        row.get::<_, Option<String>>(5)?,
        row.get::<_, i64>(6)?,
    ))
}

fn policy_rule_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<PolicyRuleRow> {
    Ok((
        row.get::<_, String>(0)?,
        row.get::<_, String>(1)?,
        row.get::<_, String>(2)?,
        row.get::<_, String>(3)?,
        row.get::<_, Option<String>>(4)?,
        row.get::<_, String>(5)?,
        row.get::<_, Option<String>>(6)?,
        row.get::<_, i64>(7)?,
    ))
}

fn policy_expression_rule_row(
    row: &rusqlite::Row<'_>,
) -> rusqlite::Result<PolicyExpressionRuleRow> {
    Ok((
        row.get::<_, String>(0)?,
        row.get::<_, String>(1)?,
        row.get::<_, String>(2)?,
        row.get::<_, Option<String>>(3)?,
        row.get::<_, String>(4)?,
        row.get::<_, Option<String>>(5)?,
        row.get::<_, i64>(6)?,
    ))
}

fn purpose_policy_rule_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<PurposePolicyRuleRow> {
    Ok((
        row.get::<_, String>(0)?,
        row.get::<_, String>(1)?,
        row.get::<_, String>(2)?,
        row.get::<_, String>(3)?,
        row.get::<_, Option<String>>(4)?,
        row.get::<_, String>(5)?,
        row.get::<_, Option<String>>(6)?,
        row.get::<_, i64>(7)?,
    ))
}

fn agent_capability_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<AgentCapabilityRow> {
    Ok((
        row.get::<_, String>(0)?,
        row.get::<_, String>(1)?,
        row.get::<_, Option<String>>(2)?,
        row.get::<_, String>(3)?,
        row.get::<_, Option<String>>(4)?,
        row.get::<_, i64>(5)?,
    ))
}

fn purpose_capability_rule_row(
    row: &rusqlite::Row<'_>,
) -> rusqlite::Result<PurposeCapabilityRuleRow> {
    Ok((
        row.get::<_, String>(0)?,
        row.get::<_, String>(1)?,
        row.get::<_, Option<String>>(2)?,
        row.get::<_, String>(3)?,
        row.get::<_, Option<String>>(4)?,
        row.get::<_, i64>(5)?,
    ))
}

fn operation_capability_rule_row(
    row: &rusqlite::Row<'_>,
) -> rusqlite::Result<OperationCapabilityRuleRow> {
    Ok((
        row.get::<_, String>(0)?,
        row.get::<_, String>(1)?,
        row.get::<_, Option<String>>(2)?,
        row.get::<_, String>(3)?,
        row.get::<_, Option<String>>(4)?,
        row.get::<_, i64>(5)?,
    ))
}

fn fragment_policy_label_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<FragmentPolicyLabelRow> {
    Ok((
        row.get::<_, String>(0)?,
        row.get::<_, i64>(1)?,
        row.get::<_, i64>(2)?,
        row.get::<_, String>(3)?,
        row.get::<_, Option<String>>(4)?,
        row.get::<_, String>(5)?,
        row.get::<_, Option<String>>(6)?,
        row.get::<_, i64>(7)?,
    ))
}

fn collect_policy_label_rows(
    rows: rusqlite::MappedRows<
        '_,
        impl FnMut(&rusqlite::Row<'_>) -> rusqlite::Result<PolicyLabelRow>,
    >,
) -> AnfsResult<Vec<PolicyLabelRow>> {
    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}

fn collect_policy_rule_rows(
    rows: rusqlite::MappedRows<
        '_,
        impl FnMut(&rusqlite::Row<'_>) -> rusqlite::Result<PolicyRuleRow>,
    >,
) -> AnfsResult<Vec<PolicyRuleRow>> {
    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}

fn collect_policy_expression_rule_rows(
    rows: rusqlite::MappedRows<
        '_,
        impl FnMut(&rusqlite::Row<'_>) -> rusqlite::Result<PolicyExpressionRuleRow>,
    >,
) -> AnfsResult<Vec<PolicyExpressionRuleRow>> {
    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}

fn collect_purpose_policy_rule_rows(
    rows: rusqlite::MappedRows<
        '_,
        impl FnMut(&rusqlite::Row<'_>) -> rusqlite::Result<PurposePolicyRuleRow>,
    >,
) -> AnfsResult<Vec<PurposePolicyRuleRow>> {
    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}

fn collect_agent_capability_rows(
    rows: rusqlite::MappedRows<
        '_,
        impl FnMut(&rusqlite::Row<'_>) -> rusqlite::Result<AgentCapabilityRow>,
    >,
) -> AnfsResult<Vec<AgentCapabilityRow>> {
    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}

fn collect_purpose_capability_rule_rows(
    rows: rusqlite::MappedRows<
        '_,
        impl FnMut(&rusqlite::Row<'_>) -> rusqlite::Result<PurposeCapabilityRuleRow>,
    >,
) -> AnfsResult<Vec<PurposeCapabilityRuleRow>> {
    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}

fn collect_operation_capability_rule_rows(
    rows: rusqlite::MappedRows<
        '_,
        impl FnMut(&rusqlite::Row<'_>) -> rusqlite::Result<OperationCapabilityRuleRow>,
    >,
) -> AnfsResult<Vec<OperationCapabilityRuleRow>> {
    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}

fn collect_fragment_policy_label_rows(
    rows: rusqlite::MappedRows<
        '_,
        impl FnMut(&rusqlite::Row<'_>) -> rusqlite::Result<FragmentPolicyLabelRow>,
    >,
) -> AnfsResult<Vec<FragmentPolicyLabelRow>> {
    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}
