import { ApiClient } from './request'

export interface ScreeningOrderBy { field: string; direction: 'asc' | 'desc' }
export interface ScreeningRunReq {
  market?: 'CN'
  date?: string | null
  adj?: 'qfq' | 'hfq' | 'none'
  conditions: any
  order_by?: ScreeningOrderBy[]
  limit?: number
  offset?: number
}

export interface ScreeningRunItem {
  code: string
  close?: number
  pct_chg?: number
  amount?: number
  ma20?: number
  rsi14?: number
  kdj_k?: number
  kdj_d?: number
  kdj_j?: number
  dif?: number
  dea?: number
  macd_hist?: number
}

export interface ScreeningRunResp { total: number; items: ScreeningRunItem[] }

// 筛选字段配置
export interface FieldInfo {
  name: string
  display_name: string
  field_type: string
  data_type: string
  description: string
  supported_operators: string[]
}

export interface FieldConfigResponse {
  fields: Record<string, FieldInfo>
  categories: Record<string, string[]>
}

// 行业列表响应
export interface IndustryOption {
  value: string
  label: string
  count: number
}

export interface IndustriesResponse {
  industries: IndustryOption[]
  total: number
}

export interface AICandidatePricePlan {
  observation_zone?: number[] | null
  entry_strategy: 'pullback' | 'breakout' | 'reference'
  entry_strategy_label: string
  entry_price?: number | null
  breakout_price?: number | null
  stop_price?: number | null
  target_price?: number | null
  distance_to_entry_pct?: number | null
  price_condition_met: boolean
  risk_blocked: boolean
  entry_status:
    | 'waiting_pullback'
    | 'waiting_breakout'
    | 'price_ready'
    | 'price_ready_risk_blocked'
    | 'invalidated'
    | 'quote_unavailable'
    | 'plan_unavailable'
  entry_status_label: string
  entry_guidance: string
  status: string
}

export interface AICandidateHorizonPlan extends Partial<AICandidatePricePlan> {
  horizon: 'short' | 'swing' | 'position'
  horizon_label: string
  validity: string
  basis?: string
  reward_risk_ratio?: number
}

export interface AICandidateRiskFlag {
  code: string
  severity: string
  message: string
}

export type AICandidateObjectiveTier = 'core' | 'related' | 'non_core'
export type AICandidateActionability =
  | 'ready_now'
  | 'condition_order'
  | 'blocked'
  | 'invalidated'
  | 'target_reached'
  | 'expired'
  | 'quote_unavailable'
  | 'incomplete'

export interface AICandidateItem {
  code: string
  name: string
  market: string
  priority: number
  research_status: AICandidateActionability
  research_status_label: string
  actionability: AICandidateActionability
  actionability_label: string
  can_add_to_favorites: boolean
  condition_order_ready: boolean
  rank_score: number
  objective_id?: string
  objective_label?: string
  objective_tier?: AICandidateObjectiveTier
  objective_tier_label?: string
  objective_segment?: string
  objective_match_score?: number
  objective_reason?: string
  reference_price?: number | null
  pct_change?: number | null
  trade_at?: string | null
  quote_source?: string | null
  quote_checked_at?: string | null
  price_plan: AICandidatePricePlan
  plans?: Record<'short' | 'swing' | 'position', AICandidateHorizonPlan>
  reason_summary: string
  evidence: string[]
  risk_flags: AICandidateRiskFlag[]
  favorite_status: 'not_added' | 'in_favorites'
  source: 'public_full_market'
  is_reference_only: true
  position_sizing?: {
    status: string
    suggested_quantity?: number
    suggested_amount?: number
    suggested_position_pct?: number
    planned_loss_amount?: number
    planned_loss_pct_of_assets?: number
    reason?: string
  }
  portfolio_allocation?: {
    rank: number
    status: 'allocated' | 'watch_only' | 'budget_exhausted' | 'market_blocked'
    reason: string
    quantity: number
    amount: number
    position_pct: number
    planned_loss_amount: number
    planned_loss_pct_of_assets: number
  }
  stock_profile?: {
    status: 'verified' | 'incomplete' | 'missing'
    confidence: 'high' | 'medium' | 'low' | 'missing'
    industry?: string | null
    main_business?: string | null
    source?: string | null
    evidence?: Array<{ field: string; value: string; source: string }>
  }
  portfolio_gate?: {
    blocked: boolean
    reason_code: string
    market_regime?: string
    available_new_exposure_pct?: number
  }
  performance?: {
    baseline_price?: number
    latest_price?: number
    return_since_generated_pct?: number
    max_return_pct?: number
    min_return_pct?: number
    observation_count?: number
    target_hit_at?: string
    stop_hit_at?: string
    shadow_trade?: {
      status?: string
      entry_triggered_at?: string
      entry_price?: number
      quantity?: number
      net_pnl?: number
      net_return_pct?: number
      alpha_pct?: number
    }
  }
}

export interface AICandidateRun {
  run_id: string
  status: 'completed'
  source: 'public_full_market'
  generated_at: string
  plan_expires_at?: string
  expires_at: string
  candidate_count: number
  candidates: AICandidateItem[]
  actionability_counts: Record<AICandidateActionability, number>
  quote_refreshed_at?: string
  account?: {
    total_assets: number
    available_cash: number
    current_exposure_pct: number
  }
  portfolio_plan?: {
    status: string
    capital_budget: number
    allocated_amount: number
    remaining_capital: number
    allocated_exposure_pct: number
    loss_budget: number
    total_planned_loss: number
    remaining_loss_budget: number
    total_planned_loss_pct: number
    allocated_position_count: number
    watch_only_count: number
  }
  objective?: {
    id: string
    label: string
    description: string
    candidate_counts: Record<AICandidateObjectiveTier, number>
    portfolio: {
      green_new_exposure_cap_pct: number
      yellow_new_exposure_cap_pct: number
      reserve_cash_pct: number
      preferred_single_symbol_pct: number
      hard_single_symbol_cap_pct: number
      per_position_loss_budget_pct: number
      total_new_position_loss_budget_pct: number
      policy_source?: string
      market_regime?: string
      total_assets?: number
      current_exposure_pct?: number
      new_exposure_cap_pct?: number
      available_new_exposure_pct?: number
    }
  }
  discovery: {
    benchmark_trade_date?: string | null
    universe_count?: number | null
    eligible_count?: number | null
    selected_count?: number | null
    technical_passed_count?: number | null
    earnings_selected_count?: number | null
    total_coverage_ratio?: number | null
  }
  market: {
    session?: string | null
    is_trading_hours?: boolean | null
    local_time?: string | null
    regime?: 'green' | 'yellow' | 'red'
    decision?: string | null
    reason_code?: string | null
    domestic_regime?: 'green' | 'yellow' | 'red'
    regime_reason?: string
    macro_risk?: {
      status: string
      regime: 'green' | 'yellow' | 'red'
      score?: number | null
      factors?: Array<{ key: string; value: number; signal: string }>
      checked_at?: string
      source?: string
    }
  }
  context: {
    horizon: string
    technical_status?: string | null
    earnings_status?: string | null
  }
  disclaimer: string
}

export interface AICandidateJob {
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  created_at: string
  started_at?: string
  completed_at?: string
  run_id?: string
  result?: AICandidateRun
  error?: { code?: string; message?: string; stage?: string }
}

export interface AICandidatePerformance {
  sample_count: number
  triggered_count?: number
  closed_count?: number
  average_return_pct?: number | null
  positive_count: number
  closed_win_rate_pct?: number | null
  target_hit_count: number
  stop_hit_count: number
}

export interface AddAICandidatesResult {
  run_id: string
  requested_count: number
  added_count: number
  added_codes: string[]
  already_exists_codes: string[]
  failed_codes: string[]
}

export const screeningApi = {
  run: (payload: ScreeningRunReq, options?: { timeout?: number }) =>
    ApiClient.post<ScreeningRunResp>('/api/screening/run', payload, { timeout: options?.timeout ?? 120000 }),
  runAiCandidates: (maxCandidates: number = 5) =>
    ApiClient.post<AICandidateJob>(
      '/api/screening/ai-candidates/run',
      { max_candidates: maxCandidates },
      { timeout: 30000 }
    ),
  getAiCandidateJob: (jobId: string) =>
    ApiClient.get<AICandidateJob>(`/api/screening/ai-candidates/jobs/${jobId}`),
  getLatestAiCandidates: (refresh: boolean = true) =>
    ApiClient.get<AICandidateRun | null>(`/api/screening/ai-candidates/latest?refresh=${refresh}`),
  getAiCandidatePerformance: () =>
    ApiClient.get<AICandidatePerformance>('/api/screening/ai-candidates/performance'),
  addAiCandidatesToFavorites: (runId: string, codes: string[]) =>
    ApiClient.post<AddAICandidatesResult>(
      `/api/screening/ai-candidates/${runId}/favorites`,
      { codes }
    ),
  getFields: () => ApiClient.get<FieldConfigResponse>('/api/screening/fields'),
  getIndustries: () => ApiClient.get<IndustriesResponse>('/api/screening/industries')
}
