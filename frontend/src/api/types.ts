export type User = {
  id: number;
  email: string;
  role?: "admin" | "user";
  created_at?: string;
};

export type Source = {
  id: number;
  name: string;
  enabled: boolean;
  url: string | null;
  rss_url: string | null;
  parse_strategy: string | null;
  source_type: string | null;
  update_frequency: string | null;
  listing_url: string | null;
  listing_strategy: string | null;
  listing_selector: string | null;
  article_link_selector: string | null;
  article_date_selector: string | null;
  network_region: "auto" | "ru" | "external";
  network_profile: "direct" | "proxy" | "browser";
  last_ru_probe_status: string | null;
  last_external_probe_status: string | null;
  external_required_reason: string | null;
  external_cooldown_until: string | null;
  last_seen_article_url: string | null;
  last_seen_published_at: string | null;
  // Архив источника: не опрашивается И его статьи не показываются в ленте.
  // NULL = активен. Отдельно от enabled — выключение ленту не чистило.
  archived_at: string | null;
};

export type SourceHealth = {
  id: number;
  verdict: "ok" | "stale" | "no_articles" | "disabled";
  articles: number | null;
  last_article_at: string | null;
};

export type SourceCandidate = {
  id: number;
  url: string;
  normalized_domain: string;
  name: string | null;
  candidate_type: string | null;
  status: "new" | "researching" | "test_parsing" | "needs_human_review" | "approved" | "rejected" | "paused";
  discovered_by: string;
  discovery_reason: string | null;
  topic: string | null;
  expected_tags_json?: string[];
  confidence: number | null;
  tested_articles: number;
  relevant_articles: number;
  avg_score: number | null;
  duplicate_count: number;
  noise_count: number;
  recommended_action: "add" | "test_more" | "reject" | "human_review" | null;
  review_comment: string | null;
  approved_source_id: number | null;
  created_at: string | null;
  updated_at: string | null;
};

export type SourceCandidateTriageRow = SourceCandidate & {
  triage_priority: number;
  triage_reason: string;
};

export type SourceCandidateArticle = {
  id: number;
  candidate_id: number;
  title: string;
  url: string;
  published_at: string | null;
  raw_text?: string | null;
  language: string | null;
  text_chars: number;
  prefilter_keep: boolean | null;
  prefilter_reason: string | null;
  relevant: boolean | null;
  relevance_reason: string | null;
  relevance_model: string | null;
  summary: string | null;
  tag_id: number | null;
  tag_confidence: number | null;
  tag_rationale: string | null;
  total_score: number | null;
  score_label: string | null;
  processing_status: "new" | "ok" | "rejected" | "error";
  error_message: string | null;
  created_at: string | null;
  updated_at: string | null;
};

export type SourceCandidateEvaluationResult = {
  candidate_id: number;
  task_id: number;
  url: string;
  collected: {
    inserted_or_updated: number;
    errors: number;
  };
  processed: {
    processed: number;
    relevant: number;
    rejected: number;
    errors: number;
  };
  metrics: {
    tested_articles: number;
    relevant_articles: number;
    avg_score: number | null;
    duplicate_count: number;
    noise_count: number;
  };
  recommended_action: SourceCandidate["recommended_action"];
  next_status: SourceCandidate["status"];
  review_comment: string;
  duration_ms: number;
};

export type SourceCandidateApprovePayload = {
  name?: string | null;
  source_type?: string;
  parse_strategy?: "rss" | "request" | "playwright" | null;
  enabled?: boolean;
  category?: string | null;
  priority?: number;
  network_region?: "auto" | "ru" | "external";
  scrape_after_approve?: boolean;
};

export type SourceCandidatePatchPayload = {
  status?: SourceCandidate["status"];
  recommended_action?: SourceCandidate["recommended_action"];
  review_comment?: string | null;
};

export type AgentPlanAction = {
  action_type: "discover_sources" | "review_source_candidate" | "recheck_source" | "tune_source_frequency" | "audit_existing_source";
  priority: number;
  topic?: string | null;
  limit?: number;
  query_hints?: string[];
  memory_explanation?: {
    query_hints?: string[];
    promoted_combos?: Array<{
      query: string;
      domain: string;
      score: number;
      status: string;
      reason?: string | null;
    }>;
    muted_combos?: Array<{
      query: string;
      domain: string;
      score: number;
      status: string;
      reason?: string | null;
    }>;
    feedback?: Record<string, number>;
  };
  reason: string;
  policy_decision?: "auto" | "human_review" | "blocked";
  policy_reason?: string;
  requires_human_approval?: boolean;
  operator_label?: string | null;
  operator_url?: string | null;
  candidate_id?: number;
  url?: string | null;
  source_id?: number;
  source_name?: string | null;
  direction?: "increase" | "decrease" | string;
  recommended_frequency?: string | null;
  audit_status?: string;
  audit_problem_type?: string;
  audit_severity?: string;
  audit_confidence?: string;
  audit_recommendation?: string;
  audit_recommendation_label?: string;
  audit_reasons?: string[];
  audit_decision_log?: Record<string, unknown>;
};

export type AgentMemory = {
  id: number;
  memory_key: string;
  memory_type: "topic" | "domain" | "source" | "query" | "plan" | "rule" | string;
  subject: string;
  status: "active" | "muted" | "rejected" | string;
  score: number;
  facts_json: Record<string, unknown>;
  last_seen_at: string | null;
  created_at: string | null;
  updated_at: string | null;
};

export type SignalEvidence = {
  id: number;
  signal_id: number;
  article_id: number | null;
  source_url: string;
  title: string;
  title_ru: string | null;
  publisher: string | null;
  published_at: string | null;
  evidence_type: string;
  extracted_fact: string | null;
  summary_ru: string | null;
  strength: number;
  raw_payload_json: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
};

export type Signal = {
  id: number;
  signal_key: string;
  title: string;
  title_ru: string | null;
  theme: string;
  summary: string | null;
  thesis: string | null;
  transferability: string | null;
  maturity: "reject" | "watch" | "shortlist" | "proven" | string;
  confidence: number;
  score: number;
  why_now: string | null;
  why_not_noise: string | null;
  companies_json: string[] | null;
  industries_json: string[] | null;
  evidence_count: number;
  first_seen_at: string | null;
  last_seen_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  user_status?: "watch" | "digest" | "archive" | "noise" | "duplicate" | string;
  selected_for_digest?: boolean;
  user_comment?: string | null;
  user_status_updated_at?: string | null;
  feedback_count?: number;
  merged_count?: number;
  // Ревью пачки: насколько сигнал интересен на фоне соседей по прогону и почему.
  interest_score?: number | null;
  why_interesting?: string | null;
  evidence?: SignalEvidence[];
};

export type SignalPatch = {
  status?: "watch" | "digest" | "archive" | "noise" | "duplicate";
  selected_for_digest?: boolean;
  analyst_comment?: string | null;
};

export type SignalFeedbackPayload = {
  article_id?: number | null;
  signal_id?: number | null;
  signal_evidence_id?: number | null;
  source_url?: string | null;
  signal_title?: string | null;
  source?: string | null;
  comment: string;
  verdict?:
    | "strong_signal"
    | "approved"
    | "watch_later"
    | "background_material"
    | "reject"
    | "wrong_domain"
    | "merge_duplicate"
    // Прежняя шкала: с экрана убрана, но в старых записях встречается.
    | "needs_better_source"
    | "bad_translation"
    | "too_generic"
    | null;
  reason?: string | null;
  corrected_title?: string | null;
  corrected_thesis?: string | null;
  duplicate_of_signal_id?: number | null;
};

export type AgentAction = {
  id: number;
  task_id: number | null;
  action_type: string;
  input_json: Record<string, unknown>;
  output_json: Record<string, unknown>;
  cost_usd: number;
  duration_ms: number | null;
  created_at: string | null;
  task_kind: string | null;
  task_status: string | null;
  task_topic: string | null;
  decision_title?: string;
  decision_summary?: string;
  decision_tone?: "neutral" | "good" | "warning" | "bad" | string;
};

export type AgentRun = {
  id: number;
  kind: string;
  status: "running" | "ok" | "failed" | string;
  trigger: string | null;
  payload_json: Record<string, unknown>;
  result_json: Record<string, unknown>;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string | null;
  action_count: number;
  job_count: number;
  ok_job_count: number;
  failed_job_count: number;
};

export type SourceDiscoveryQualityRow = {
  subject: string;
  candidates: number;
  approved: number;
  rejected: number;
  paused: number;
  needs_human_review: number;
  test_more: number;
  tested_articles: number;
  relevant_articles: number;
  noise_count: number;
  avg_score: number | null;
  approval_rate: number;
  relevance_rate: number;
};

export type QueryMemoryRow = {
  query: string;
  topic: string | null;
  score: number;
  status: string;
  found_candidates: number;
  tested_articles: number;
  relevant_articles: number;
  avg_score: number | null;
  empty_result: boolean;
  relevance_rate: number;
  last_seen_at: string | null;
  updated_at: string | null;
};

export type SourceDiscoveryEvaluation = {
  summary: {
    candidates: number;
    candidate_decisions: number;
    candidate_agreement_rate: number;
    source_memories: number;
    sources_under_watch: number;
    high_confidence_sources: number;
    weak_rules: number;
    recent_actions: number;
  };
  candidate_recommendations: {
    total: number;
    decided: number;
    agreed: number;
    disagreed: number;
    agreement_rate: number;
    by_recommendation: Array<{
      recommendation: string;
      total: number;
      decided: number;
      agreed: number;
      disagreed: number;
      agreement_rate: number;
    }>;
    disagreements: Array<Record<string, unknown>>;
  };
  source_audit: {
    total: number;
    under_watch: number;
    confidence: Record<string, number>;
    severity: Record<string, number>;
    problems: Array<{ problem_type: string; count: number; avg_source_score: number }>;
    recommendations: Array<{ recommendation: string; label: string; count: number; avg_source_score: number }>;
    rules: Array<{
      rule: string;
      triggered: number;
      suppressed: number;
      confidence_low: number;
      avg_source_score: number;
      suppression_rate: number;
    }>;
    examples: Array<Record<string, unknown>>;
  };
  weak_rules: Array<{
    rule: string;
    triggered: number;
    suppressed: number;
    confidence_low: number;
    avg_source_score: number;
    suppression_rate: number;
  }>;
  recent_actions: {
    total: number;
    learning_events: number;
    by_type: Array<{ action_type: string; count: number }>;
  };
};

export type SourceDiscoveryReadinessIssue = {
  severity: "info" | "warning" | "blocker" | string;
  code: string;
  message: string;
};

export type SourceDiscoveryReadiness = {
  ok: boolean;
  status: "ready" | "degraded" | "blocked" | string;
  checks: Record<string, {
    ok: boolean;
    issues?: SourceDiscoveryReadinessIssue[];
    recommendations?: string[];
    [key: string]: unknown;
  }>;
  issues: SourceDiscoveryReadinessIssue[];
  recommendations: string[];
};

export type AgentPlan = {
  kind: "source_discovery_plan";
  period: {
    from: string;
    to: string;
    days: number;
  };
  inputs: {
    topic_gaps: Array<Record<string, unknown>>;
    source_quality_count: number;
    candidate_count: number;
    memory_count: number;
    query_memory_count?: number;
  };
  policy?: {
    auto: number;
    human_review: number;
    blocked: number;
  };
  learning?: {
    candidates: number;
    approved: number;
    rejected: number;
    paused: number;
    needs_human_review: number;
    test_more: number;
    approval_rate: number;
    rejection_rate: number;
  };
  actions: AgentPlanAction[];
  memory_updates: Array<Record<string, unknown>>;
  duration_ms: number;
};

export type SourceDiscoveryPlanPayload = {
  days?: number;
  target_per_topic?: number;
  topic_limit?: number;
  candidate_limit?: number;
  max_actions?: number;
  persist_memory?: boolean;
  offline?: boolean;
  evaluate?: boolean;
};

export type SourceDiscoveryDiscoverPayload = {
  topic: string;
  seed_url: string;
  limit?: number;
  offline?: boolean;
  fetch_inspection?: boolean;
  test_parse?: boolean;
};

export type SourceDiscoverySkippedCandidate = {
  url: string;
  domain: string;
  reason: string;
  source_id?: number;
  source_name?: string;
  verdict?: string | null;
  retry_after?: string;
  failure_count?: number;
  last_reason?: string;
};

export type SourceDiscoveryDiscoverResult = {
  candidates: SourceCandidate[];
  existing_sources_skipped?: SourceDiscoverySkippedCandidate[];
  cooldown_sources_skipped?: SourceDiscoverySkippedCandidate[];
  quality_gate_sources_skipped?: SourceDiscoverySkippedCandidate[];
  unavailable_sources_skipped?: SourceDiscoverySkippedCandidate[];
  parse_failed_sources_skipped?: SourceDiscoverySkippedCandidate[];
  duration_ms: number;
};

export type SourceDiscoveryLoopPayload = SourceDiscoveryPlanPayload & {
  goal?: string;
  max_iterations?: number;
  fetch_inspection?: boolean;
  test_parse?: boolean;
  dry_run?: boolean;
  auto_evaluate?: boolean;
  article_limit?: number;
  max_daily_loop_runs?: number;
  max_daily_candidates?: number;
  max_daily_evaluations?: number;
};

export type SourceDiscoveryLoopObservation = {
  action_type?: string;
  topic?: string | null;
  priority?: number | null;
  query_strategy?: string | null;
  search_status?: string | null;
  query_count?: number;
  candidate_count?: number;
  evaluated_count?: number;
  evaluation_jobs?: number;
  evaluation_errors?: number;
  relevant_articles?: number;
  avg_score?: number | null;
  task_id?: number | null;
};

export type SourceDiscoveryLoopIteration = {
  iteration: number;
  policy?: Record<string, unknown> | null;
  learning?: Record<string, unknown> | null;
  action_count: number;
  auto_action_count: number;
  human_review_count: number;
  observations: SourceDiscoveryLoopObservation[];
};

export type SourceDiscoveryLoopReflection = {
  worked_topics: Array<Record<string, unknown>>;
  empty_topics: Array<Record<string, unknown>>;
  strong_strategies: Array<Record<string, unknown>>;
  weak_strategies: Array<Record<string, unknown>>;
  next_hints: Array<Record<string, unknown>>;
  summary: Record<string, unknown>;
};

export type SourceDiscoveryLoopResult = {
  run_id: number | null;
  goal: string;
  iterations: SourceDiscoveryLoopIteration[];
  total_candidates: number;
  empty_iterations: number;
  terminal_reason: string;
  reflection?: SourceDiscoveryLoopReflection;
  budget?: Record<string, unknown>;
  dry_run?: boolean;
  duration_ms: number;
};

export type SourceDiagnostics = {
  verdict?: string;
  candidate_count?: number;
  post_count?: number;
  entry_count?: number;
  listing_probe?: ProbePayload;
  preview_probe?: ProbePayload;
  rss_probe?: ProbePayload;
  article_checks?: DiagnosticArticleCheck[];
  candidates?: DiagnosticListItem[];
  posts?: DiagnosticListItem[];
  entries?: DiagnosticListItem[];
};

export type ProbePayload = {
  status?: number;
  bytes?: number;
  proxy?: string;
};

export type DiagnosticArticleCheck = {
  verdict?: string;
  text_chars?: number;
  candidate_url?: string;
};

export type DiagnosticListItem = {
  title?: string;
  url?: string;
};

export type SourcePatch = Partial<
  Pick<
    Source,
    | "enabled"
    | "url"
    | "rss_url"
    | "parse_strategy"
    | "update_frequency"
    | "listing_url"
    | "listing_strategy"
    | "listing_selector"
    | "article_link_selector"
    | "article_date_selector"
    | "network_region"
    | "network_profile"
  >
>;

export type Article = {
  id: number;
  title: string;
  url: string;
  source: string;
  tag: string;
  summary: string;
  score: number;
  rating: string;
  status: "new" | "digest" | "archive" | "noise" | "duplicate";
  language: string | null;
  date: string | null;
  collected: string | null;
  raw_text_chars: number;
  text_truncated: boolean;
  relevant: boolean | null;
  relevance_reason: string | null;
  digest: boolean;
  future_date?: boolean;
  published_at?: string | null;
  score_explanation?: string | null;
  tag_rationale?: string | null;
  score_items?: ScoreItem[];
};

export type ScoreItem = {
  name: string;
  final_score: number;
  rationale?: string | null;
};

export type DashboardStats = {
  total_articles: number;
  // Весь объём базы (все статьи, включая отсев и вычищенные) — только под плитку «Всего».
  all_articles?: number;
  with_summary: number;
  processed_articles: number;
  // Почищено — статьи, убранные из выдачи перепроверкой релевантности (pending_deletion).
  // Даёт сходимость: всего = почищено + остаётся в работе.
  cleaned_articles?: number;
  selected_for_digest: number;
  avg_score: number;
  sources: number;
  // Счётчики по статусам — по ВСЕЙ базе (а не по загруженной странице), пер-юзерно.
  status_counts?: Record<Article["status"], number>;
};

export type BacklogTaskStatus = "new" | "in_progress" | "done" | "paused" | "rejected";

export type BacklogTask = {
  id: string;
  section: "plan" | "tech" | "inbox";
  priority: string;
  title: string;
  status: BacklogTaskStatus;
  status_label: string;
  updated: string;
  area?: string | null;
  details?: string | null;
  due_date?: string | null;
  comments?: BacklogComment[];
};

export type BacklogComment = {
  id: string;
  author: string;
  text: string;
  created_at: string;
};

export type BacklogPayload = {
  tasks: BacklogTask[];
  counts: Record<BacklogTaskStatus, number>;
  backlog_path: string;
  updated_at: string;
};

export type BackgroundJob = {
  id: number;
  kind: string;
  queue: string;
  execution_region: string;
  capability: string | null;
  agent_run_id?: number | null;
  status: "queued" | "running" | "ok" | "failed";
  progress: number;
  attempts: number;
  max_attempts: number;
  payload: Record<string, unknown>;
  result: Record<string, unknown>;
  error: string | null;
  run_after: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
};

export type MaintenanceStatus = {
  retention: {
    stale_minutes: number;
    background_job_days: number;
    export_job_days: number;
  };
  expired_sessions: number;
  stale_running_jobs: number;
  cleanup_candidates: {
    background_jobs: number;
    export_jobs: number;
  };
  external_queues: ExternalQueueStatus;
};

export type ExternalQueueRow = {
  queue_name: string;
  queued: number;
  running: number;
  failed: number;
  ok: number;
  oldest_queued_at: string | null;
  last_heartbeat_at: string | null;
};

export type ExternalQueueStatus = {
  totals: {
    queued: number;
    running: number;
    failed: number;
    ok: number;
    oldest_queued_at: string | null;
    last_heartbeat_at: string | null;
    expired_leases: number;
  };
  queues: ExternalQueueRow[];
};

export type MaintenanceCleanupResult = {
  expired_sessions: number;
  background_jobs: number;
  background_job_days: number;
  export_jobs: number;
  export_job_days: number;
};

export type ReadinessBenchmarkCheck = {
  name: string;
  runs: number;
  rows: number;
  p50_ms: number;
  p95_ms: number;
  max_ms: number;
  status: "ok" | "warn";
};

export type ReadinessBenchmarkReport = {
  iterations: number;
  warn_ms: number;
  params: {
    articles_limit: number;
    source_limit: number;
    jobs_limit: number;
    month: string | null;
    digest_limit: number;
    min_score: number;
  };
  benchmarks: ReadinessBenchmarkCheck[];
  counts: Record<string, number>;
  warnings: string[];
};

export type ArticlePatch = {
  status?: Article["status"];
  selected_for_digest?: boolean;
  analyst_comment?: string | null;
};

export type DigestContentItem = {
  article_id?: number;
  category: string;
  title: string;
  summary: string;
  url: string;
  source?: string;
  published_at?: string | null;
  image_url?: string;
  tag?: string;
  score?: number | null;
  score_label?: string | null;
};

export type DigestContent = {
  month: string | null;
  title: string;
  issue?: {
    title?: string;
    period?: string;
    preheader?: string;
    intro?: string;
    news_title?: string;
    read_more_label?: string;
    empty_summary_text?: string;
    preview_empty_text?: string;
  };
  hero?: {
    badge?: string;
    headline?: string;
    subtitle?: string;
    image_url?: string;
  };
  news: DigestContentItem[];
  footer?: {
    contact_text?: string;
    contact_email?: string;
    note?: string;
    socials?: DigestBrandingSocial[];
  };
};

export type DigestBrandingSocial = {
  label: string;
  accent: string;
  text: string;
};

export type DigestHighlightRules = {
  analytics_source_keywords: string[];
  analytics_category_keywords: string[];
  business_category_keywords: string[];
  cards: DigestHighlightCard[];
};

export type DigestHighlightCard = {
  metric: "total" | "analytics" | "business";
  icon: "doc" | "chart" | "people";
  prefix: string;
  suffix: string;
  noun_one: string;
  noun_few: string;
  noun_many: string;
};

export type DigestBranding = {
  header: {
    brand_text: string;
    brand_suffix: string;
    department_text: string;
  };
  hero: {
    badge: string;
    headline: string;
    subtitle: string;
    image_url: string;
  };
  issue: {
    title_template: string;
    title_template_with_month: string;
    period_label_all: string;
    preheader: string;
    intro_template: string;
    intro_template_with_month: string;
    highlights_title: string;
    news_title: string;
    read_more_label: string;
    empty_summary_text: string;
    preview_empty_text: string;
  };
  footer: {
    contact_text: string;
    contact_email: string;
    note: string;
    socials: DigestBrandingSocial[];
  };
  highlights: DigestHighlightRules;
};

export type MonthlyDigestDraft = {
  id: number;
  month: string;
  title: string;
  status: string;
  items: Array<{
    article_id: number;
    sort_order?: number;
    section?: string | null;
    editor_note?: string | null;
  }>;
};

export type DigestDraftSaveResult = {
  id: number;
  month: string;
  title: string;
  status: string;
  items: number;
  content_items?: number;
};

export type ScoringCriterion = {
  id: number | null;
  name: string;
  description: string | null;
  weight: number;
  keywords_json: string[];
  keywords_en_json: string[];
  sort_order: number;
  enabled?: boolean;
};

export type Tag = {
  id: number | null;
  parent_name: string | null;
  name: string;
  name_en?: string | null;
  description: string | null;
  keywords_json: string[];
  keywords_en_json: string[];
  negative_keywords_json?: string[];
  enabled: boolean;
  sort_order: number;
};

export type CreateSourcePayload = {
  name: string;
  url: string;
  rss_url?: string;
  priority?: number;
  category?: string | null;
  update_frequency?: string | null;
};

export type ScrapeResponse = {
  ok: boolean;
  stats: {
    added: number;
    attempted: number;
  };
};

export type ManualArticleImportPayload = {
  url: string;
  source_id?: number | null;
  process?: boolean;
  offline?: boolean;
};

export type ManualArticleImportResult = {
  ok: boolean;
  article: {
    id: number;
    source_id: number;
    source_name: string;
    duplicate: boolean;
    title: string;
    fetch_method: string;
    full_text_status: string | null;
    full_text_method: string | null;
    full_text_chars: number;
  };
  job?: BackgroundJob;
};

export type AuthResponse = {
  ok: boolean;
  user: User;
};

// --- Месячная статистика платформы (раздел «Статистика», admin-only) ---
export type MonthlyPlatformRow = {
  month: string;
  collected: number;
  relevant: number;
  rejected: number;
  hidden: number;
  summarized: number;
  scored: number;
  avg_score: number | null;
  digest_ready: number;
};

export type MonthlyAiCostRow = {
  month: string;
  model: string;
  runs: number;
  cost_usd: number;
};

export type MonthlyActivityRow = {
  month: string;
  user_id: number;
  email: string;
  status: string;
  marks: number;
};

export type MonthlyStats = {
  months: number;
  platform: MonthlyPlatformRow[];
  ai_cost: MonthlyAiCostRow[];
  activity: MonthlyActivityRow[];
  activity_scope: string;
};

// --- Приём файлов: документы пользователя (экран «Материалы») ---
export type DocumentStatus = "uploaded" | "parsed" | "processing" | "ready" | "failed";

export type UploadedDocument = {
  id: number;
  filename: string;
  kind: string | null;
  size_bytes: number | null;
  status: DocumentStatus | string;
  error_message: string | null;
  anchor_unit: string | null;
  anchor_count: number | null;
  empty_anchors: number | null;
  text_chars: number | null;
  essence: string | null;
  doc_type: string | null;
  publisher: string | null;
  fact_count: number | null;
  created_at: string | null;
};

// summary_json/claims_json приходят из БД как JSON: с сервера обещан список пунктов,
// но тип здесь unknown НАМЕРЕННО — карточка не должна падать, если в поле окажется
// объект или строка, а не список (см. ErrorBoundary вокруг карточки).
export type DocumentCard = {
  doc_type: string | null;
  publisher: string | null;
  doc_date: string | null;
  // Откуда взята дата: «документ» / «имя файла» / «нет». Название файла мог поменять
  // кто угодно, поэтому в карточке это помечается (тикет #50).
  date_source?: string | null;
  language: string | null;
  essence: string | null;
  summary_json: unknown;
  claims_json: unknown;
};

export type DocumentFact = {
  value: unknown;
  unit: string | null;
  context: string | null;
  anchor: number | string | null;
  verified: boolean;
};

export type DocumentDetails = {
  ok: boolean;
  document: UploadedDocument;
  card: DocumentCard | null;
  facts: DocumentFact[];
};

export type DocumentListResult = {
  ok: boolean;
  documents: UploadedDocument[];
};

export type DocumentUploadResult = {
  ok: boolean;
  duplicate: boolean;
  document: UploadedDocument;
  job_id?: number;
};

// Обратная связь человека: оценки 1–5 + быстрая причина + комментарий.
// Поля выведены из того, как заказчик уже пишет ОС руками (чат 10.09):
// корректировка заголовка, актуальность статьи, качество источника, правки перевода.
export type FeedbackEntry = {
  id: number;
  user_id: number;
  article_id: number | null;
  source_id: number | null;
  reason: string | null;
  usefulness: number | null;
  translation: number | null;
  source_quality: number | null;
  comment: string | null;
  created_at: string;
  updated_at: string;
};

export type FeedbackReason = { value: string; label: string };

export type FeedbackSourceSummary = {
  source_id: number;
  source_name: string;
  entries: number;
  avg_usefulness: number | null;
  avg_translation: number | null;
  avg_source_quality: number | null;
  off_topic: number;
  incomplete_text: number;
  duplicate: number;
  bad_translation: number;
  good: number;
};
