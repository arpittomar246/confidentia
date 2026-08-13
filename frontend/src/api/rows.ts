/**
 * Row shapes this console expects.
 *
 * The one place in the frontend that is not checked against the OpenAPI schema --
 * see the note in `client.ts` for why the API's responses are `unknown`. These are
 * transcribed from the `SELECT` lists in `src/confidentia/api/routers/`; if a
 * column is renamed there, this file is what goes stale, and the symptom is an
 * empty cell rather than a crash.
 *
 * Every numeric field is `| null`. That is not defensive typing: the platform
 * genuinely returns nulls that mean different things, and the components are
 * required to distinguish them. A null `precision` means the denominator was zero;
 * a null cell in a mart is k-anonymity suppression; a null `finished_at` means the
 * run has not finished. None of the three is "0".
 */

export interface Envelope<T> {
  items: T[];
  page?: { limit: number; offset: number; returned: number };
  total?: number;
}

export interface Meta {
  service: string;
  environment: string;
  read_only: boolean;
  auth_mode: string;
  tee: { backend: string; hardware_rooted: boolean; label: "MODELLED" | "MEASURED" };
}

export interface Run {
  run_id: string;
  run_type: string;
  source: string | null;
  tenant: string;
  dataset: string;
  status: string;
  attempt: number;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  records_read: number | null;
  records_written: number | null;
  records_rejected: number | null;
  rejection_rate: number | null;
  error_type: string | null;
  /** Only present on `/runs/{run_id}` -- deliberately absent from the list. */
  error_message?: string | null;
}

export interface RunStats {
  by_status: {
    status: string;
    runs: number;
    median_duration_ms: number | null;
    max_duration_ms: number | null;
    records_written: number | null;
  }[];
  by_type: { run_type: string; runs: number; failed: number }[];
}

export interface Score {
  score_id?: number;
  run_id?: string;
  tenant: string;
  dataset: string;
  layer: string;
  overall_score: number | null;
  completeness: number | null;
  validity: number | null;
  consistency: number | null;
  uniqueness: number | null;
  integrity: number | null;
  timeliness: number | null;
  rows_total: number | null;
  rows_failing: number | null;
  rules_evaluated?: number | null;
  rules_failed: number | null;
  promotion_blocked: boolean;
  scored_at: string;
}

export const DIMENSIONS = [
  "completeness",
  "validity",
  "consistency",
  "uniqueness",
  "integrity",
  "timeliness",
] as const;

export type Dimension = (typeof DIMENSIONS)[number];

export interface Rule {
  rule_id: string;
  tenant: string;
  dataset: string;
  dimension: string;
  family: string | null;
  field: string | null;
  severity: string;
  description: string | null;
  is_derived: boolean;
  is_active: boolean;
  created_at: string;
}

export interface Failure {
  result_id: number;
  run_id: string;
  rule_id: string;
  tenant: string;
  dataset: string;
  layer: string;
  rows_evaluated: number | null;
  rows_failed: number | null;
  pass_rate: number | null;
  evaluated_at: string;
  dimension: string | null;
  family: string | null;
  field: string | null;
  severity: string | null;
  description: string | null;
}

export interface Freshness {
  tenant: string;
  dataset: string;
  layer: string;
  max_event_ts: string | null;
  max_ingest_ts: string | null;
  observed_at: string;
  lag_minutes: number | null;
  sla_minutes: number | null;
  breached: boolean;
}

export interface QuarantineRow {
  quarantine_id: number;
  run_id: string;
  tenant: string;
  dataset: string;
  layer: string;
  business_key: string | null;
  batch_digest: string | null;
  reason_code: string;
  failed_rule: string | null;
  severity: string | null;
  quarantined_at: string;
  replayed_at: string | null;
  /** Auditor-only, on `/quality/quarantine/{id}` alone. */
  redacted_record?: Record<string, unknown>;
}

export interface QuarantineSummary {
  tenant: string;
  dataset: string;
  reason_code: string;
  severity: string | null;
  records: number;
  replayed: number;
  latest: string;
}

export interface Evaluation {
  tenant: string;
  dataset: string;
  layer: string;
  family: string;
  true_positives: number;
  false_positives: number;
  false_negatives: number;
  truth_claims: number;
  detected_claims: number;
  precision: number | null;
  recall: number | null;
  f1: number | null;
  /** False means the generator injects this defect and no rule covers it. */
  rule_exists: boolean;
  evaluated_at: string;
}

export interface EvaluationCaveat {
  applies_to: { layer?: string; family?: string };
  note: string;
}

export interface EvaluationSummary {
  tenant: string;
  dataset: string;
  layer: string;
  tp: number;
  fp: number;
  fn: number;
  families: number;
  families_uncovered: number;
  families_excluded: number;
  precision: number | null;
  recall: number | null;
  f1: number | null;
  /**
   * The same micro-average with the contaminated Bronze `duplicate_record` family
   * removed. **This is the one to report as rule quality** — see the endpoint's
   * docstring. On `financial/transaction` the difference is F1 0.251 vs 0.837.
   */
  precision_excl: number | null;
  recall_excl: number | null;
  f1_excl: number | null;
}

export interface GateDecision {
  gate_id?: number;
  run_id?: string;
  tenant: string;
  dataset: string;
  layer: string;
  overall_score: number | null;
  threshold: number | null;
  blocked?: boolean;
  decision: string;
  reason: string | null;
  overridden: boolean;
  decided_at: string;
}

export interface IngestBatch {
  batch_digest: string;
  source: string;
  tenant: string;
  dataset: string;
  file_count: number | null;
  row_count: number | null;
  bytes: number | null;
  status: string;
  first_seen_at: string;
  ingested_at: string | null;
  /** Above 1 means a redelivery was recognised and skipped -- not an error. */
  seen_count: number;
  run_id: string | null;
}

export interface Watermark {
  source: string;
  tenant: string;
  dataset: string;
  /** LSN, timestamp and file sequence are all opaque strings; this discriminates. */
  watermark_kind: string;
  watermark_value: string;
  previous_value: string | null;
  updated_at: string;
  run_id: string | null;
}

export interface IngestSummary {
  tenant: string;
  dataset: string;
  source: string;
  batches: number;
  redelivered: number;
  rows_ingested: number | null;
  bytes_ingested: number | null;
  latest_ingest: string | null;
}

export interface LineageEntry {
  seq: number;
  entry_digest: string;
  previous_digest?: string | null;
  run_id: string | null;
  tenant: string;
  dataset: string;
  layer: string;
  transformation: string;
  record_count: number | null;
  merkle_root: string;
  code_measurement: string;
  hardware_rooted: boolean;
  recorded_at: string;
  /** Auditor-only, on `/lineage/entries/{seq}`. */
  payload?: Record<string, unknown>;
  signature?: string;
  public_key_pem?: string;
}

export interface LineageHead {
  head: {
    seq: number;
    entry_digest: string;
    tenant: string;
    dataset: string;
    layer: string;
    recorded_at: string;
  } | null;
  entries: number;
}

export interface LineageVerification {
  ok: boolean;
  entries_checked: number;
  summary: string;
  failures: { seq: number; index: number; reason: string; detail: string }[];
  scope: {
    chain_links: boolean;
    signatures: boolean;
    columns_match_payload: boolean;
    /** Always false over HTTP: deep verification needs Spark and is CLI-only. */
    lake_contents_rehashed: boolean;
    note: string;
  };
}

export interface KeyRelease {
  audit_id: number;
  requested_at: string;
  measurement: string;
  tenant: string;
  key_ids: string[] | null;
  granted: boolean;
  reason: string | null;
  evidence_digest: string | null;
  run_id: string | null;
  tee_backend: string;
  hardware_rooted: boolean;
}

export interface KeyReleaseSummary {
  tenant: string;
  granted: boolean;
  reason: string | null;
  tee_backend: string;
  hardware_rooted: boolean;
  requests: number;
  latest: string;
}

export interface AttestationPolicy {
  measurement: string;
  description: string | null;
  tenants: string[] | null;
  approved_by: string | null;
  approved_at: string;
  revoked_at: string | null;
  tee_backend: string;
  hardware_rooted: boolean;
  notes: string | null;
}

export interface DisclosureRecord {
  disclosure_id?: number;
  published_at: string;
  run_id: string | null;
  tenant: string;
  mart?: string;
  min_cell_count: number;
  total_cells: number;
  primary_suppressed: number;
  /** Without this, a small cell is recoverable by subtracting from a margin. */
  complementary_suppressed: number;
  differential_privacy: boolean;
  epsilon: number | null;
  rows_published: number;
}

export interface Restatement {
  restatement_id: number;
  restated_at: string;
  run_id: string | null;
  tenant: string;
  mart: string;
  partitions_restated: number;
  trigger_reason: string | null;
  oldest_partition: string | null;
  newest_partition: string | null;
  lag_days: number | null;
}

export interface MartListing {
  items: { mart: string; rows: number; disclosure: DisclosureRecord | null }[];
  note: string;
}

export interface MartSchema {
  mart: string;
  columns: { column_name: string; data_type: string; is_nullable: string }[];
}

/** Mart rows are genuinely dynamic: Gold drops and recreates the table each publish. */
export type MartRow = Record<string, string | number | boolean | null>;
