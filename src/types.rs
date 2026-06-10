use serde::{Deserialize, Serialize};

pub(crate) type RefHistoryRow = (
    String,
    String,
    String,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
    i64,
);
pub(crate) type EventEdgeRow = (String, String, String, Option<String>);
pub(crate) type EventRecord = (
    String,
    String,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
    i64,
    Vec<EventEdgeRow>,
);
pub(crate) type RefViewRow = (String, String, String, String, String, i64);
pub(crate) type RefViewCheckpointRow = (
    String,
    String,
    i64,
    Option<String>,
    bool,
    i64,
    String,
    Option<String>,
    i64,
);
pub(crate) type RefViewCheckpointVerificationRow = (String, bool, String, String, i64, i64);
pub(crate) type MaterializedViewRow = (String, String, String, i64);
pub(crate) type WorktreeMaterializeRow = (String, String, String, i64);
pub(crate) type WorktreeCommitRow = (String, String, Option<String>);
pub(crate) type WorktreeReadinessRow = (String, bool, String, i64);
pub(crate) type CachedWorkingSetResult = (String, String, i64, bool);
pub(crate) type CachedWorkingSetRow = (String, String, String, String, i64, i64);
pub(crate) type NodeChunkRow = (i64, i64, i64, String);
pub(crate) type DerivedIndexRepairResult = (i64, i64);
pub(crate) type RepairPlanRow = (String, String, String, String);
pub(crate) type CompactionPlanRow = (String, i64, String, String);
pub(crate) type ArchivalReadinessRow = (String, bool, String, i64);
pub(crate) type VacuumDatabaseResult = (i64, i64, bool);
pub(crate) type InlineBlobCompactionResult = (i64, i64, i64, bool);
pub(crate) type OrphanObjectCleanupResult = (i64, i64, i64, bool);
pub(crate) type SchemaStatusRow = (i64, String, i64, String);
pub(crate) type SchemaMigrationPlanRow = (i64, String, String, String);
pub(crate) type SchemaMigrationApplyResult = (i64, i64, bool);
pub(crate) type VectorSearchRow = (String, String, f64);
pub(crate) type ChunkVectorSearchRow = (String, String, i64, i64, i64, f64);
pub(crate) type ExportBundleResult = (String, i64, i64, i64);
pub(crate) type ImportBundleResult = (String, i64, i64, i64);
pub(crate) type GcResultRow = (String, i64, String, String);
pub(crate) type GcPinRow = (String, String, String, String, Option<String>, i64);
pub(crate) type RetentionPolicyRow = (String, String, bool, Option<i64>, Option<i64>, String, i64);
pub(crate) type PolicyLabelRow = (
    String,
    String,
    String,
    Option<String>,
    String,
    Option<String>,
    i64,
);
pub(crate) type PolicyRuleRow = (
    String,
    String,
    String,
    String,
    Option<String>,
    String,
    Option<String>,
    i64,
);
pub(crate) type PurposePolicyRuleRow = (
    String,
    String,
    String,
    String,
    Option<String>,
    String,
    Option<String>,
    i64,
);
pub(crate) type AgentCapabilityRow = (String, String, Option<String>, String, Option<String>, i64);
pub(crate) type PurposeCapabilityRuleRow =
    (String, String, Option<String>, String, Option<String>, i64);
pub(crate) type OperationCapabilityRuleRow =
    (String, String, Option<String>, String, Option<String>, i64);
pub(crate) type PolicyExpressionRuleRow = (
    String,
    String,
    String,
    Option<String>,
    String,
    Option<String>,
    i64,
);
pub(crate) type FragmentPolicyLabelRow = (
    String,
    i64,
    i64,
    String,
    Option<String>,
    String,
    Option<String>,
    i64,
);
pub(crate) type JsonFieldSpanRow = (String, i64, i64, String);
pub(crate) type MarkdownFieldSpanRow = (String, i64, i64, String);
pub(crate) type MarkdownFieldValueRow = (String, String, String);
pub(crate) type MarkdownSectionSpanRow = (String, i64, i64, String);
pub(crate) type AnswerEvidenceCoverageRow = (String, String, bool, i64);
pub(crate) type AnswerTokenAccountingRow = (String, String, i64, i64, i64, i64, i64);
pub(crate) type TokenCostProfileRow = (String, String, i64, i64, i64, String, Option<String>, i64);
pub(crate) type AnswerCostEstimateRow = (String, String, i64, i64, i64, i64, i64, i64, String);
pub(crate) type AnswerQuoteSupportRow = (String, String, bool, i64);
pub(crate) type EventListRow = (
    i64,
    String,
    String,
    Option<String>,
    Option<String>,
    i64,
    i64,
    i64,
);
pub(crate) type QueryRefRow = (
    String,
    String,
    String,
    String,
    i64,
    String,
    Option<String>,
    i64,
    Option<i64>,
    Option<String>,
    Option<String>,
);
pub(crate) type LineageGraphRow = (
    i64,
    String,
    String,
    String,
    String,
    Option<String>,
    String,
    String,
    Option<String>,
);
pub(crate) type RunRow = (
    String,
    Option<String>,
    Option<String>,
    String,
    Option<String>,
    i64,
    i64,
    Option<i64>,
);
pub(crate) type ManifestChildRecord = (
    String,
    String,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<i64>,
);

#[derive(Clone)]
pub(crate) struct RefRecord {
    pub(crate) node_id: String,
    pub(crate) ref_kind: String,
    pub(crate) state: String,
    pub(crate) ref_version: i64,
}

#[derive(Debug, Serialize, Deserialize)]
pub(crate) struct ManifestDoc {
    pub(crate) schema: String,
    pub(crate) kind: String,
    pub(crate) children: Vec<ManifestChild>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub(crate) struct ManifestChild {
    pub(crate) path: String,
    pub(crate) node_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) role: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) media_type: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) digest: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) size: Option<i64>,
}

#[derive(Debug, Serialize, Deserialize)]
pub(crate) struct BundleEvent {
    pub(crate) event_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) source_seq: Option<i64>,
    pub(crate) kind: String,
    pub(crate) agent_id: Option<String>,
    pub(crate) run_id: Option<String>,
    pub(crate) tool_call_id: Option<String>,
    pub(crate) workspace_id: Option<String>,
    pub(crate) payload_json: Option<String>,
    pub(crate) created_at: i64,
}

#[derive(Debug, Serialize, Deserialize)]
pub(crate) struct BundleEventEdge {
    pub(crate) event_id: String,
    pub(crate) direction: String,
    pub(crate) node_id: String,
    pub(crate) role: String,
    pub(crate) logical_path: Option<String>,
}

#[derive(Debug, Serialize, Deserialize)]
pub(crate) struct BundleNode {
    pub(crate) node_id: String,
    pub(crate) blob_hash: String,
    pub(crate) kind: String,
    pub(crate) media_type: Option<String>,
    pub(crate) metadata_json: Option<String>,
    pub(crate) created_at: i64,
}

#[derive(Debug, Serialize, Deserialize)]
pub(crate) struct BundleBlob {
    pub(crate) hash: String,
    pub(crate) size: i64,
    pub(crate) storage_kind: String,
    pub(crate) object_path: Option<String>,
}

#[derive(Debug, Serialize, Deserialize)]
pub(crate) struct BundleRefEvent {
    pub(crate) ref_name: String,
    pub(crate) event_id: String,
    pub(crate) old_node_id: Option<String>,
    pub(crate) new_node_id: Option<String>,
    pub(crate) old_state: Option<String>,
    pub(crate) new_state: Option<String>,
}

#[derive(Debug, Serialize, Deserialize)]
pub(crate) struct ReplayManifest {
    pub(crate) schema: String,
    pub(crate) event_id: String,
    pub(crate) prefix: Option<String>,
    pub(crate) include_inactive: bool,
    pub(crate) materialized_at: i64,
    pub(crate) files: Vec<ReplayManifestFile>,
}

#[derive(Debug, Serialize, Deserialize)]
pub(crate) struct ReplayManifestFile {
    pub(crate) ref_name: String,
    pub(crate) relative_path: String,
    pub(crate) node_id: String,
    pub(crate) size: i64,
}

#[derive(Debug, Serialize, Deserialize)]
pub(crate) struct BundleSignature {
    pub(crate) scheme: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) signer_id: Option<String>,
    pub(crate) payload_sha256: String,
    pub(crate) signature: String,
}

#[derive(Debug, Serialize, Deserialize)]
pub(crate) struct EventBundle {
    pub(crate) schema: String,
    pub(crate) root_event_id: String,
    pub(crate) exported_at: i64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) bundle_checksum: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) bundle_signature: Option<BundleSignature>,
    pub(crate) events: Vec<BundleEvent>,
    pub(crate) event_edges: Vec<BundleEventEdge>,
    pub(crate) nodes: Vec<BundleNode>,
    pub(crate) blobs: Vec<BundleBlob>,
    pub(crate) ref_events: Vec<BundleRefEvent>,
}
