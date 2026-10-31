export type Role = "employee" | "sales" | "hr" | "admin";

export interface User {
  id: string;
  username: string;
  display_name: string;
  role: Role;
  can_upload: boolean;
  is_admin: boolean;
}

export interface Health {
  status: string;
  llm_provider: string;
  mock_mode: boolean;
  model: string | null;
  embedding_backend: string;
  roles: Role[];
}

export interface DocumentInfo {
  id: string;
  title: string;
  original_filename: string;
  source_id: string;
  file_type: "pdf" | "html" | "markdown" | "txt";
  product: string | null;
  product_version: string | null;
  effective_date: string | null;
  uploaded_at: string;
  updated_at: string;
  status: "processing" | "ready" | "failed" | "quarantined" | "deleted";
  approval_status: "pending" | "approved" | "rejected";
  page_count: number | null;
  chunk_count: number;
  content_hash: string;
  collection: string;
  security_flags: string[];
  uploader_id: string;
  allowed_roles?: Role[];
  approved_by?: string | null;
  baseline_hash?: string | null;
}

export type AnswerState =
  | "answered"
  | "partially_answered"
  | "clarification_needed"
  | "conflicting_evidence"
  | "not_found";

export interface Citation {
  label: string;
  number: number;
  chunk_id: string;
  document_id: string;
  title: string;
  section: string | null;
  page: number | null;
  section_anchor: string;
  product_version: string | null;
  effective_date: string | null;
  source_id: string;
  excerpt: string;
  passage_url: string;
  download_url: string;
  injection_flag: boolean;
}

export interface Conflict {
  kind: "version_difference" | "contradiction";
  description: string;
  citations: number[];
  facts: { value: string; title: string; version: string | null }[];
}

export interface RetrievedDebug {
  chunk_id: string;
  document_id: string;
  title: string;
  section: string;
  page: number | null;
  product_version: string | null;
  dense_score: number;
  bm25_score: number;
  fused_score: number;
  term_coverage: number;
  exact_code_match: boolean;
  injection_flag: boolean;
  used_as_evidence: boolean;
  rank: number;
  text: string;
}

export interface AskResponse {
  id: string;
  question: string;
  state: AnswerState;
  answer: { text: string; citations: number[] }[];
  message: string | null;
  missing_information: string[];
  clarification_question: string | null;
  conflicts: Conflict[];
  citations: Citation[];
  warnings: string[];
  generator: string | null;
  mock_mode: boolean;
  security_flags: string[];
  cached: boolean;
  generation_error?: string | null;
  debug?: {
    top_k: number;
    evidence_count: number;
    asked_version: string | null;
    notes: string[];
    thresholds: Record<string, number>;
    retrieved: RetrievedDebug[];
  };
}

export interface Passage {
  chunk_id: string;
  document_id: string;
  title: string;
  section: string | null;
  section_anchor: string;
  page: number | null;
  text: string;
  product_version: string | null;
  source_id: string;
  injection_flag: boolean;
}

export interface AuditEntry {
  id: number;
  ts: string;
  request_id: string | null;
  user_id: string | null;
  action: string;
  resource: string | null;
  decision: string | null;
  details: string | null;
}
