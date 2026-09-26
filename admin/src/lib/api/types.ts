/**
 * Request/response shapes of the FastAPI content API.
 *
 * Hand-mirrored from api/app/schemas/{common,content,generation}.py — keep the
 * two in step when a schema changes (field names and Literal vocabularies are
 * copied verbatim). Only what the admin panel calls is typed here.
 */

// ---------- closed vocabularies (api/app/schemas/common.py) ----------

export type Locale = "en" | "es";
export const LOCALES: readonly Locale[] = ["en", "es"];

export type Region = "us" | "latam" | "global";
export const REGIONS: readonly Region[] = ["us", "latam", "global"];

export type GradeBand = "g1_g3" | "g4_g6" | "g7_g9" | "g10_g12" | "adult";
export const GRADE_BANDS: readonly GradeBand[] = ["g1_g3", "g4_g6", "g7_g9", "g10_g12", "adult"];

export type QuestionStatus = "pending" | "live" | "archived" | "rejected";
export type QuestionSource = "seed" | "ai" | "user" | "manual";
export type GenerationKind = "category" | "study_pack";
export type GenerationStatus = "queued" | "running" | "succeeded" | "partial" | "failed";

export type BulkAction = "approve" | "reject" | "archive";

export const OPTION_COUNT = 4;
export const DIFFICULTIES = [1, 2, 3, 4, 5] as const;

// ---------- categories ----------

export interface CategoryTranslation {
  locale: Locale;
  name: string;
  description: string | null;
}

export interface Category {
  id: string;
  slug: string;
  icon: string | null;
  sort_order: number;
  is_active: boolean;
  created_at: string;
  translations: CategoryTranslation[];
}

export interface CategoryTranslationIn {
  name: string;
  description?: string | null;
}

/** CategoryCreate: both locales are required. */
export interface CategoryCreate {
  slug: string;
  icon?: string | null;
  sort_order?: number;
  translations: Record<Locale, CategoryTranslationIn>;
}

/** CategoryUpdate: every field optional; a locale that is sent is replaced, one omitted is kept. */
export interface CategoryUpdate {
  icon?: string | null;
  sort_order?: number;
  is_active?: boolean;
  translations?: Partial<Record<Locale, CategoryTranslationIn>>;
}

// ---------- questions / review queue ----------

export interface QuestionTranslation {
  locale: Locale;
  stem: string;
  options: string[];
  explanation: string | null;
}

export interface Question {
  id: string;
  category_id: string;
  difficulty: number;
  grade_band: GradeBand | null;
  region: Region;
  tags: string[];
  correct_index: number;
  status: QuestionStatus;
  source: QuestionSource;
  pack_id: string | null;
  generation_job_id: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  created_at: string;
  updated_at: string;
  translations: QuestionTranslation[];
}

/** QuestionTranslationIn: text for one locale, sent whole (PATCH replaces a locale that is sent). */
export interface QuestionTranslationIn {
  stem: string;
  options: string[];
  explanation?: string | null;
}

/** QuestionUpdate: every field optional; omitted means "keep". */
export interface QuestionUpdate {
  translations?: Partial<Record<Locale, QuestionTranslationIn>>;
  correct_index?: number;
  difficulty?: number;
  grade_band?: GradeBand | null;
  region?: Region;
  tags?: string[];
}

/** QuestionListQuery: GET /admin/questions filters (all AND-ed). */
export interface QuestionListQuery {
  status?: QuestionStatus;
  category?: string; // slug
  locale?: Locale;
  difficulty?: number;
  job_id?: string;
  page?: number;
  page_size?: number;
}

export interface QuestionPage {
  items: Question[];
  page: number;
  page_size: number;
  total: number;
}

export interface QuestionBulkFailure {
  id: string;
  detail: string;
}

export interface QuestionBulkResult {
  updated: string[];
  failed: QuestionBulkFailure[];
}

// ---------- generation jobs (api/app/schemas/generation.py) ----------

export interface GenerationParams {
  category_slug: string;
  count: number;
  difficulty_min: number;
  difficulty_max: number;
  grade_bands: GradeBand[];
  region: Region;
  locales: Locale[];
  style_notes: string | null;
}

export interface GenerationStats {
  rejections: Record<string, number>;
  emitted: number;
  repeated: number;
  repeat_rate: number;
  chunks_total: number;
  chunks_failed: number;
  chunk_errors: string[];
  input_tokens: number;
  output_tokens: number;
}

export interface GenerationJob {
  id: string;
  kind: GenerationKind;
  requested_by: string | null;
  prompt: string | null;
  params: GenerationParams;
  status: GenerationStatus;
  model: string | null;
  requested_count: number | null;
  produced_count: number;
  accepted_count: number;
  rejected_count: number;
  cost_cents: number | null;
  error: string | null;
  stats: GenerationStats;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

/** POST /admin/generate: the prompt plus the params the admin confirmed. */
export interface GenerationRequest {
  prompt: string;
  params: GenerationParams;
  kind?: GenerationKind;
}

/** POST /admin/generate/parse → params to confirm; `notes` lists what the parser adjusted. */
export interface ParseResponse {
  params: GenerationParams;
  notes: string[];
}

// ---------- health views (api/app/schemas/telemetry.py) ----------

export type HealthView = "easy" | "suspect" | "dead";
export const HEALTH_VIEWS: readonly HealthView[] = ["easy", "suspect", "dead"];

export interface QuestionStats {
  question_id: string;
  serves: number;
  correct: number;
  incorrect: number;
  timeouts: number;
  absents: number;
  reports: number;
  avg_response_ms: number | null;
  last_served_at: string | null;
}

/** HealthRow: the stats row, the view's ratio (correct/serves; timeouts/serves for dead) and the question. */
export interface HealthRow extends QuestionStats {
  ratio: number;
  question: Question;
}

export interface HealthPage {
  view: HealthView;
  items: HealthRow[];
  page: number;
  page_size: number;
  total: number;
}

export type StatusCounts = Record<QuestionStatus, number>;

export interface HealthSummary {
  pending_backlog: number;
  by_status: StatusCounts;
  by_category: { slug: string; counts: StatusCounts }[];
  by_locale: Record<Locale, StatusCounts>;
  health: Record<HealthView, number>;
}

// ---------- error bodies ----------

/** One entry of FastAPI's request-validation 422 body. */
export interface ValidationErrorItem {
  loc: (string | number)[];
  msg: string;
  type: string;
}

/**
 * FastAPI `detail`: a string for HTTPException(detail=str) (the service-level
 * 422s and every 409), a list for Pydantic request validation.
 */
export type ErrorDetail = string | ValidationErrorItem[];
