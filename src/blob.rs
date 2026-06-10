use rusqlite::{params, Connection, OptionalExtension, Transaction};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use uuid::Uuid;

use crate::{AnfsError, AnfsResult, INLINE_THRESHOLD};

pub(crate) enum BlobStorage {
    Inline(Vec<u8>),
    File(String),
}

pub(crate) fn sha256_hex(content: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(content);
    hex::encode(hasher.finalize())
}

pub(crate) fn materialize_blob(
    objects_dir: &Path,
    hash: &str,
    content: &[u8],
) -> AnfsResult<BlobStorage> {
    if content.len() < INLINE_THRESHOLD {
        return Ok(BlobStorage::Inline(content.to_vec()));
    }

    let object_path = materialize_blob_file(objects_dir, hash, content)?;
    Ok(BlobStorage::File(object_path.to_string_lossy().to_string()))
}

pub(crate) fn materialize_blob_file(
    objects_dir: &Path,
    hash: &str,
    content: &[u8],
) -> AnfsResult<PathBuf> {
    let object_path = object_path(objects_dir, hash);
    if !object_path.exists() {
        if let Some(parent) = object_path.parent() {
            fs::create_dir_all(parent)?;
        }
        let tmp_path = object_path.with_extension(format!("tmp-{}", Uuid::new_v4()));
        fs::write(&tmp_path, content)?;
        fs::rename(&tmp_path, &object_path)?;
    }
    Ok(object_path)
}

pub(crate) fn object_path(objects_dir: &Path, hash: &str) -> PathBuf {
    objects_dir.join(&hash[0..2]).join(&hash[2..4]).join(hash)
}

pub(crate) fn file_blob_path(objects_dir: &Path, uri: &str) -> PathBuf {
    let path = PathBuf::from(uri);
    if path.is_absolute() {
        path
    } else {
        objects_dir.join(path)
    }
}

pub(crate) fn insert_blob(
    tx: &Transaction<'_>,
    hash: &str,
    size: i64,
    storage: &BlobStorage,
) -> AnfsResult<()> {
    match storage {
        BlobStorage::Inline(bytes) => {
            tx.execute(
                "INSERT OR IGNORE INTO blobs
                 (hash, size, storage_kind, storage_uri, inline_content)
                 VALUES (?1, ?2, 'inline', NULL, ?3)",
                params![hash, size, bytes],
            )?;
            if tx.changes() == 0 {
                ensure_existing_blob_matches(tx, hash, size, storage)?;
            }
        }
        BlobStorage::File(uri) => {
            tx.execute(
                "INSERT OR IGNORE INTO blobs
                 (hash, size, storage_kind, storage_uri, inline_content)
                 VALUES (?1, ?2, 'file', ?3, NULL)",
                params![hash, size, uri],
            )?;
            if tx.changes() == 0 {
                ensure_existing_blob_matches(tx, hash, size, storage)?;
            }
        }
    }
    Ok(())
}

pub(crate) fn ensure_existing_blob_matches(
    conn: &Connection,
    hash: &str,
    size: i64,
    storage: &BlobStorage,
) -> AnfsResult<()> {
    let existing: Option<(i64, String, Option<String>, Option<Vec<u8>>)> = conn
        .query_row(
            "SELECT size, storage_kind, storage_uri, inline_content
             FROM blobs
             WHERE hash = ?1",
            params![hash],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .optional()?;
    let expected = match storage {
        BlobStorage::Inline(bytes) => (size, "inline".to_string(), None, Some(bytes.clone())),
        BlobStorage::File(uri) => (size, "file".to_string(), Some(uri.clone()), None),
    };
    if existing != Some(expected) {
        return Err(AnfsError::StorageCorruption(format!(
            "existing blob {hash} conflicts with CAS metadata for incoming content"
        )));
    }
    Ok(())
}

pub(crate) fn orphan_object_files(
    conn: &Connection,
    objects_dir: &Path,
    limit: Option<i64>,
) -> AnfsResult<Vec<(String, i64, PathBuf)>> {
    if let Some(value) = limit {
        if !(1..=10_000).contains(&value) {
            return Err(AnfsError::PolicyDenied(
                "orphan object cleanup limit must be between 1 and 10000".to_string(),
            ));
        }
    }

    let mut owned_paths = HashSet::new();
    let mut stmt = conn.prepare(
        "
        SELECT storage_uri
        FROM blobs
        WHERE storage_kind = 'file'
          AND storage_uri IS NOT NULL
        ",
    )?;
    let rows = stmt.query_map([], |row| row.get::<_, String>(0))?;
    for row in rows {
        let uri = row?;
        owned_paths.insert(normalize_object_path(&file_blob_path(objects_dir, &uri))?);
    }

    let mut out = Vec::new();
    scan_orphan_object_dir(objects_dir, objects_dir, &owned_paths, &mut out)?;
    out.sort_by(|left, right| left.0.cmp(&right.0));
    if let Some(value) = limit {
        out.truncate(value as usize);
    }
    Ok(out)
}

fn scan_orphan_object_dir(
    objects_dir: &Path,
    dir: &Path,
    owned_paths: &HashSet<PathBuf>,
    out: &mut Vec<(String, i64, PathBuf)>,
) -> AnfsResult<()> {
    if !dir.exists() {
        return Ok(());
    }
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        let path = entry.path();
        let file_type = entry.file_type()?;
        if file_type.is_dir() {
            scan_orphan_object_dir(objects_dir, &path, owned_paths, out)?;
            continue;
        }
        if !file_type.is_file() {
            continue;
        }
        let Some(hash) = object_hash_from_path(objects_dir, &path)? else {
            continue;
        };
        let normalized = normalize_object_path(&path)?;
        if owned_paths.contains(&normalized) {
            continue;
        }
        let size = entry.metadata()?.len() as i64;
        out.push((hash, size, path));
    }
    Ok(())
}

fn object_hash_from_path(objects_dir: &Path, path: &Path) -> AnfsResult<Option<String>> {
    let Ok(relative) = path.strip_prefix(objects_dir) else {
        return Ok(None);
    };
    let parts: Vec<_> = relative.components().collect();
    if parts.len() != 3 {
        return Ok(None);
    }
    let mut strings = Vec::new();
    for part in parts {
        let std::path::Component::Normal(value) = part else {
            return Ok(None);
        };
        let Some(value) = value.to_str() else {
            return Ok(None);
        };
        strings.push(value);
    }
    let hash = strings[2];
    if strings[0].len() == 2
        && strings[1].len() == 2
        && hash.len() == 64
        && hash.starts_with(strings[0])
        && hash[2..4] == *strings[1]
        && hash.chars().all(|ch| ch.is_ascii_hexdigit())
    {
        Ok(Some(hash.to_ascii_lowercase()))
    } else {
        Ok(None)
    }
}

fn normalize_object_path(path: &Path) -> AnfsResult<PathBuf> {
    if path.exists() {
        Ok(path.canonicalize()?)
    } else if let Some(parent) = path.parent() {
        if parent.exists() {
            let file_name = path.file_name().ok_or_else(|| {
                AnfsError::StorageCorruption(format!(
                    "object path {} has no file name",
                    path.display()
                ))
            })?;
            Ok(parent.canonicalize()?.join(file_name))
        } else {
            Ok(path.to_path_buf())
        }
    } else {
        Ok(path.to_path_buf())
    }
}
