use crate::{AnfsError, AnfsResult};

pub(crate) fn normalize_workspace(workspace: &str) -> String {
    workspace.trim_end_matches('/').to_string()
}

pub(crate) fn validate_workspace_name(workspace: &str) -> AnfsResult<String> {
    let normalized = normalize_workspace(workspace);
    let Some(name) = normalized.strip_prefix("ws:") else {
        return Err(AnfsError::PolicyDenied(format!(
            "workspace name {normalized} must start with ws:"
        )));
    };
    if name.is_empty()
        || normalized.len() > 128
        || name.starts_with(['.', '-', '_'])
        || name.ends_with(['.', '-', '_'])
        || !name
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || ch == '.' || ch == '-' || ch == '_')
    {
        return Err(AnfsError::PolicyDenied(format!(
            "unsafe workspace name {normalized}; expected ws:<ascii-name> using letters, digits, '.', '-', or '_'"
        )));
    }
    Ok(normalized)
}

pub(crate) fn workspace_ref_name(workspace: &str, logical_path: &str) -> String {
    format!(
        "{}/{}",
        workspace.trim_end_matches('/'),
        logical_path.trim_start_matches('/')
    )
}

pub(crate) fn base_child_ref_name(base: &str, logical_path: &str) -> String {
    format!(
        "{}/{}",
        base.trim_end_matches('/'),
        logical_path.trim_start_matches('/')
    )
}

pub(crate) fn infer_ref_kind(ref_name: &str) -> &str {
    if ref_name.starts_with("artifact:") {
        "artifact"
    } else if ref_name.starts_with("memory:") {
        "memory"
    } else if ref_name.starts_with("ws:") {
        "workspace"
    } else {
        "resource"
    }
}

pub(crate) fn checkout_logical_path(base_prefix: &str, source_ref: &str) -> String {
    let child_prefix = format!("{base_prefix}/");
    if let Some(suffix) = source_ref.strip_prefix(&child_prefix) {
        return suffix.to_string();
    }
    source_ref
        .rsplit('/')
        .next()
        .filter(|segment| !segment.is_empty())
        .unwrap_or(source_ref)
        .to_string()
}

pub(crate) fn workspace_logical_path(workspace: &str, workspace_ref: &str) -> Option<String> {
    let workspace_prefix = workspace.trim_end_matches('/');
    let child_prefix = format!("{workspace_prefix}/");
    workspace_ref
        .strip_prefix(&child_prefix)
        .map(|suffix| suffix.to_string())
}
