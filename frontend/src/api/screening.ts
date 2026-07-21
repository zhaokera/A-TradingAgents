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

export interface AICandidateRiskFlag {
  code: string
  severity: string
  message: string
}

export type AICandidateObjectiveTier = 'core' | 'related' | 'non_core'

export interface AICandidateItem {
  code: string
  name: string
  market: string
  priority: number
  research_status: 'observe'
  research_status_label: string
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
  price_plan: AICandidatePricePlan
  reason_summary: string
  evidence: string[]
  risk_flags: AICandidateRiskFlag[]
  favorite_status: 'not_added' | 'in_favorites'
  source: 'public_full_market'
  is_reference_only: true
}

export interface AICandidateRun {
  run_id: string
  status: 'completed'
  source: 'public_full_market'
  generated_at: string
  expires_at: string
  candidate_count: number
  candidates: AICandidateItem[]
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
  }
  context: {
    horizon: string
    technical_status?: string | null
    earnings_status?: string | null
  }
  disclaimer: string
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
    ApiClient.post<AICandidateRun>(
      '/api/screening/ai-candidates/run',
      { max_candidates: maxCandidates },
      { timeout: 120000 }
    ),
  getLatestAiCandidates: () =>
    ApiClient.get<AICandidateRun | null>('/api/screening/ai-candidates/latest'),
  addAiCandidatesToFavorites: (runId: string, codes: string[]) =>
    ApiClient.post<AddAICandidatesResult>(
      `/api/screening/ai-candidates/${runId}/favorites`,
      { codes }
    ),
  getFields: () => ApiClient.get<FieldConfigResponse>('/api/screening/fields'),
  getIndustries: () => ApiClient.get<IndustriesResponse>('/api/screening/industries')
}
