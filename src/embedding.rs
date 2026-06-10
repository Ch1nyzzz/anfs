use rusqlite::{params, Connection, OptionalExtension};

use crate::{
    ensure_node_exists, node_range_hidden, now_millis, AnfsError, AnfsResult,
    ChunkVectorSearchRow, VectorSearchRow, VisibilityPolicy,
};

pub(crate) fn set_node_embedding(
    conn: &Connection,
    node_id: &str,
    model: &str,
    vector: &[f64],
) -> AnfsResult<()> {
    ensure_node_exists(conn, node_id)?;
    let model = validate_embedding_model(model)?;
    let norm = vector_norm(vector)?;
    let vector_json = serde_json::to_string(vector).map_err(|err| {
        AnfsError::StorageCorruption(format!("failed to serialize embedding vector: {err}"))
    })?;
    conn.execute(
        "INSERT INTO node_embeddings
         (node_id, model, dimensions, vector_json, norm, indexed_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)
         ON CONFLICT(node_id, model) DO UPDATE SET
             dimensions = excluded.dimensions,
             vector_json = excluded.vector_json,
             norm = excluded.norm,
             indexed_at = excluded.indexed_at",
        params![
            node_id,
            model,
            vector.len() as i64,
            vector_json,
            norm,
            now_millis()
        ],
    )?;
    Ok(())
}

pub(crate) fn node_embedding(
    conn: &Connection,
    node_id: &str,
    model: &str,
) -> AnfsResult<Option<Vec<f64>>> {
    let model = validate_embedding_model(model)?;
    let row: Option<(i64, String, f64)> = conn
        .query_row(
            "SELECT dimensions, vector_json, norm
             FROM node_embeddings
             WHERE node_id = ?1 AND model = ?2",
            params![node_id, model],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional()?;
    let Some((dimensions, vector_json, norm)) = row else {
        return Ok(None);
    };
    let vector = parse_embedding_vector(&vector_json)?;
    validate_stored_embedding(node_id, model, dimensions, norm, &vector)?;
    Ok(Some(vector))
}

pub(crate) fn vector_search(
    conn: &Connection,
    model: &str,
    query_vector: &[f64],
    state: &str,
    limit: i64,
    policy_label_excludes: &[String],
) -> AnfsResult<Vec<VectorSearchRow>> {
    let model = validate_embedding_model(model)?;
    let query_norm = vector_norm(query_vector)?;
    let state = validate_vector_search_state(state)?;
    if limit <= 0 {
        return Err(AnfsError::PolicyDenied(
            "vector_search limit must be positive".to_string(),
        ));
    }
    let visibility = VisibilityPolicy::new(policy_label_excludes)?;
    let mut stmt = conn.prepare(
        "
        SELECT r.ref_name, r.node_id, ne.dimensions, ne.vector_json, ne.norm
        FROM refs r
        JOIN node_embeddings ne ON ne.node_id = r.node_id
        WHERE r.state = ?1
          AND ne.model = ?2
        ORDER BY r.ref_name
        ",
    )?;
    let rows = stmt.query_map(params![state, model], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, i64>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, f64>(4)?,
        ))
    })?;
    let mut scored = Vec::new();
    for row in rows {
        let (ref_name, node_id, dimensions, vector_json, norm) = row?;
        if visibility.ref_node_blocked(conn, &ref_name, &node_id)? {
            continue;
        }
        let vector = parse_embedding_vector(&vector_json)?;
        validate_stored_embedding(&node_id, model, dimensions, norm, &vector)?;
        if vector.len() != query_vector.len() {
            continue;
        }
        let score = dot(query_vector, &vector) / (query_norm * norm);
        scored.push((ref_name, node_id, score));
    }
    scored.sort_by(|left, right| {
        right
            .2
            .total_cmp(&left.2)
            .then_with(|| left.0.cmp(&right.0))
            .then_with(|| left.1.cmp(&right.1))
    });
    scored.truncate(limit as usize);
    Ok(scored)
}

pub(crate) fn set_node_chunk_embedding(
    conn: &Connection,
    node_id: &str,
    chunk_size: i64,
    chunk_index: i64,
    model: &str,
    vector: &[f64],
) -> AnfsResult<()> {
    validate_chunk_embedding_key(chunk_size, chunk_index)?;
    ensure_node_chunk_exists(conn, node_id, chunk_size, chunk_index)?;
    let model = validate_embedding_model(model)?;
    let norm = vector_norm(vector)?;
    let vector_json = serde_json::to_string(vector).map_err(|err| {
        AnfsError::StorageCorruption(format!("failed to serialize chunk embedding vector: {err}"))
    })?;
    conn.execute(
        "INSERT INTO node_chunk_embeddings
         (node_id, chunk_size, chunk_index, model, dimensions, vector_json, norm, indexed_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)
         ON CONFLICT(node_id, chunk_size, chunk_index, model) DO UPDATE SET
             dimensions = excluded.dimensions,
             vector_json = excluded.vector_json,
             norm = excluded.norm,
             indexed_at = excluded.indexed_at",
        params![
            node_id,
            chunk_size,
            chunk_index,
            model,
            vector.len() as i64,
            vector_json,
            norm,
            now_millis()
        ],
    )?;
    Ok(())
}

pub(crate) fn node_chunk_embedding(
    conn: &Connection,
    node_id: &str,
    chunk_size: i64,
    chunk_index: i64,
    model: &str,
) -> AnfsResult<Option<Vec<f64>>> {
    validate_chunk_embedding_key(chunk_size, chunk_index)?;
    let model = validate_embedding_model(model)?;
    let row: Option<(i64, String, f64)> = conn
        .query_row(
            "SELECT dimensions, vector_json, norm
             FROM node_chunk_embeddings
             WHERE node_id = ?1
               AND chunk_size = ?2
               AND chunk_index = ?3
               AND model = ?4",
            params![node_id, chunk_size, chunk_index, model],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional()?;
    let Some((dimensions, vector_json, norm)) = row else {
        return Ok(None);
    };
    let vector = parse_embedding_vector(&vector_json)?;
    validate_stored_embedding(
        &format!("{node_id} chunk_size {chunk_size} chunk {chunk_index}"),
        model,
        dimensions,
        norm,
        &vector,
    )?;
    Ok(Some(vector))
}

pub(crate) fn vector_search_chunks(
    conn: &Connection,
    model: &str,
    query_vector: &[f64],
    state: &str,
    limit: i64,
    policy_label_excludes: &[String],
) -> AnfsResult<Vec<ChunkVectorSearchRow>> {
    let model = validate_embedding_model(model)?;
    let query_norm = vector_norm(query_vector)?;
    let state = validate_vector_search_state(state)?;
    if limit <= 0 {
        return Err(AnfsError::PolicyDenied(
            "vector_search_chunks limit must be positive".to_string(),
        ));
    }
    let visibility = VisibilityPolicy::new(policy_label_excludes)?;
    let mut stmt = conn.prepare(
        "
        SELECT r.ref_name,
               r.node_id,
               nce.chunk_size,
               nce.chunk_index,
               nci.offset,
               nci.size,
               nce.dimensions,
               nce.vector_json,
               nce.norm
        FROM refs r
        JOIN node_chunk_embeddings nce ON nce.node_id = r.node_id
        JOIN node_chunk_index nci
          ON nci.node_id = nce.node_id
         AND nci.chunk_size = nce.chunk_size
         AND nci.chunk_index = nce.chunk_index
        WHERE r.state = ?1
          AND nce.model = ?2
        ORDER BY r.ref_name, nce.chunk_size, nce.chunk_index
        ",
    )?;
    let rows = stmt.query_map(params![state, model], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, i64>(2)?,
            row.get::<_, i64>(3)?,
            row.get::<_, i64>(4)?,
            row.get::<_, i64>(5)?,
            row.get::<_, i64>(6)?,
            row.get::<_, String>(7)?,
            row.get::<_, f64>(8)?,
        ))
    })?;
    let mut scored = Vec::new();
    for row in rows {
        let (
            ref_name,
            node_id,
            _chunk_size,
            chunk_index,
            offset,
            size,
            dimensions,
            vector_json,
            norm,
        ) = row?;
        if visibility.ref_node_label_blocked(conn, &ref_name, &node_id)? {
            continue;
        }
        if node_range_hidden(conn, &node_id, offset, size)? {
            continue;
        }
        let vector = parse_embedding_vector(&vector_json)?;
        validate_stored_embedding(
            &format!("{node_id} chunk {chunk_index}"),
            model,
            dimensions,
            norm,
            &vector,
        )?;
        if vector.len() != query_vector.len() {
            continue;
        }
        let score = dot(query_vector, &vector) / (query_norm * norm);
        scored.push((ref_name, node_id, chunk_index, offset, size, score));
    }
    scored.sort_by(|left, right| {
        right
            .5
            .total_cmp(&left.5)
            .then_with(|| left.0.cmp(&right.0))
            .then_with(|| left.1.cmp(&right.1))
            .then_with(|| left.2.cmp(&right.2))
    });
    scored.truncate(limit as usize);
    Ok(scored)
}

fn validate_chunk_embedding_key(chunk_size: i64, chunk_index: i64) -> AnfsResult<()> {
    if chunk_size <= 0 {
        return Err(AnfsError::PolicyDenied(
            "chunk_size must be positive".to_string(),
        ));
    }
    if chunk_index < 0 {
        return Err(AnfsError::PolicyDenied(
            "chunk_index must be non-negative".to_string(),
        ));
    }
    Ok(())
}

fn ensure_node_chunk_exists(
    conn: &Connection,
    node_id: &str,
    chunk_size: i64,
    chunk_index: i64,
) -> AnfsResult<()> {
    let exists: i64 = conn.query_row(
        "
        SELECT COUNT(*)
        FROM node_chunk_index
        WHERE node_id = ?1
          AND chunk_size = ?2
          AND chunk_index = ?3
        ",
        params![node_id, chunk_size, chunk_index],
        |row| row.get(0),
    )?;
    if exists != 1 {
        return Err(AnfsError::PolicyDenied(format!(
            "node chunk embedding requires cached chunk {chunk_index} for node {node_id} chunk_size {chunk_size}"
        )));
    }
    Ok(())
}

fn validate_embedding_model(model: &str) -> AnfsResult<&str> {
    let model = model.trim();
    if model.is_empty() {
        return Err(AnfsError::PolicyDenied(
            "embedding model must not be empty".to_string(),
        ));
    }
    Ok(model)
}

fn validate_vector_search_state(state: &str) -> AnfsResult<&str> {
    match state {
        "draft" | "published" | "approved" => Ok(state),
        other => Err(AnfsError::PolicyDenied(format!(
            "unsupported vector_search state {other}"
        ))),
    }
}

fn vector_norm(vector: &[f64]) -> AnfsResult<f64> {
    if vector.is_empty() {
        return Err(AnfsError::PolicyDenied(
            "embedding vector must not be empty".to_string(),
        ));
    }
    if vector.iter().any(|value| !value.is_finite()) {
        return Err(AnfsError::PolicyDenied(
            "embedding vector values must be finite".to_string(),
        ));
    }
    let norm = vector.iter().map(|value| value * value).sum::<f64>().sqrt();
    if norm <= 0.0 {
        return Err(AnfsError::PolicyDenied(
            "embedding vector norm must be positive".to_string(),
        ));
    }
    Ok(norm)
}

fn parse_embedding_vector(vector_json: &str) -> AnfsResult<Vec<f64>> {
    serde_json::from_str(vector_json).map_err(|err| {
        AnfsError::StorageCorruption(format!("stored embedding vector is invalid JSON: {err}"))
    })
}

fn validate_stored_embedding(
    node_id: &str,
    model: &str,
    dimensions: i64,
    norm: f64,
    vector: &[f64],
) -> AnfsResult<()> {
    if dimensions <= 0 || dimensions as usize != vector.len() {
        return Err(AnfsError::StorageCorruption(format!(
            "node_embedding for {node_id} model {model} has invalid dimensions {dimensions}"
        )));
    }
    let actual_norm = vector_norm(vector)?;
    if (actual_norm - norm).abs() > 1e-9 {
        return Err(AnfsError::StorageCorruption(format!(
            "node_embedding for {node_id} model {model} has stale norm"
        )));
    }
    Ok(())
}

fn dot(left: &[f64], right: &[f64]) -> f64 {
    left.iter()
        .zip(right)
        .map(|(left, right)| left * right)
        .sum()
}
