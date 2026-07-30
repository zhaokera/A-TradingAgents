import { ApiClient } from './request'

export type DecisionAction = 'buy_now' | 'condition_order' | 'wait' | 'avoid'
export type DecisionValidationStatus =
  | 'valid'
  | 'invalid'
  | 'stale_revalidation_required'

export interface DecisionConstraint {
  code: string
  message?: string
  severity?: string
  overrideable?: boolean
  applies_to?: string[]
  source?: string
  symbol?: string
  details?: Record<string, unknown>
}

export interface DecisionEvidence {
  evidence_id: string
  kind: string
  value: unknown
}

export interface DecisionPricePlan {
  entry_strategy?: 'pullback' | 'breakout' | 'reference'
  entry_price?: number | null
  breakout_price?: number | null
  stop_price?: number | null
  target_price?: number | null
  entry_status?: string
  plan_expires_at?: string | null
}

export interface DecisionCandidate {
  symbol: string
  name: string
  identity: {
    code?: string
    name?: string
    objective_segment?: string
    objective_match_score?: number
    [key: string]: unknown
  }
  software_baseline_action: DecisionAction
  software_reason_codes: string[]
  quote: {
    price?: number | null
    source?: string
    trade_at?: string
    quote_checked_at?: string
    status?: string
    actionable?: boolean
  }
  plans: {
    short?: DecisionPricePlan
    swing?: DecisionPricePlan
    position?: DecisionPricePlan
  }
  profile?: Record<string, unknown>
  hard_constraints: DecisionConstraint[]
  soft_warnings: DecisionConstraint[]
  risk_envelope: {
    status?: string
    reason?: string
    lot_size?: number
    max_allowed_quantity?: number
    max_allowed_amount?: number
    max_position_pct?: number
    max_planned_loss_amount?: number
    max_planned_loss_pct_of_assets?: number
  }
  evidence: DecisionEvidence[]
  plan_id?: string | null
}

export interface DecisionResearchPacket {
  research_packet_id: string
  source_baseline_id: string
  as_of?: string
  created_at: string
  material_hash: string
  market_session: {
    phase?: string
    quote_freshness_required_seconds?: number
    [key: string]: unknown
  }
  account: {
    total_assets?: number
    available_cash?: number
    current_exposure_pct?: number
    [key: string]: unknown
  }
  execution_capabilities?: {
    source?: string
    condition_order?: {
      verified?: boolean
      independent_trigger_price_supported?: boolean
      separate_order_limit_price_supported?: boolean
      eligible?: boolean
    }
    market_permissions?: {
      star_market?: {
        verified?: boolean
        tradable?: boolean
        eligible?: boolean
        reason_code?: 'permission_unverified' | 'permission_denied' | null
      }
      beijing_stock_exchange?: {
        verified?: boolean
        tradable?: boolean
        eligible?: boolean
        reason_code?: 'permission_unverified' | 'permission_denied' | null
      }
    }
  }
  market: {
    combined_regime?: string
    domestic_regime?: string
    [key: string]: unknown
  }
  decision_objective: {
    max_new_positions: number
    primary_position_count: number
  }
  hard_risk_policy: {
    available_new_exposure_pct?: number
    hard_single_symbol_cap_pct?: number
    per_position_loss_budget_pct?: number
    total_new_position_loss_budget_pct?: number
    market_red_blocks_new_positions?: boolean
    [key: string]: unknown
  }
  hard_constraints: DecisionConstraint[]
  soft_warnings: DecisionConstraint[]
  candidates: DecisionCandidate[]
  data_quality: {
    unclassified_reason_codes?: string[]
    [key: string]: unknown
  }
  software_baseline: {
    baseline_id: string
    authority: 'software_baseline'
    is_final_decision: false
    market_phase?: string
    revision?: number
    summary?: Record<string, number>
    rule_version?: string
  }
  disclaimer: string
}

export interface SoftwareBaseline {
  decision_id: string
  authority: 'software_baseline'
  is_final_decision: false
  market_phase?: string
  as_of?: string
  summary?: Record<string, number>
  buy_now?: unknown[]
  condition_order?: unknown[]
  wait?: unknown[]
  avoid?: unknown[]
  [key: string]: unknown
}

export interface CodexDecisionOverride {
  warning_code: string
  reason: string
  risk_adjustment: string
}

export interface CodexDecisionSelection {
  symbol: string
  action: DecisionAction
  position_role: 'primary' | 'secondary' | 'none'
  requested_quantity?: number | null
  entry_strategy?: 'pullback' | 'breakout' | 'reference' | null
  trigger_price?: number | string | null
  order_limit_price?: number | string | null
  stop_price?: number | string | null
  target_price?: number | string | null
  expires_at?: string | null
  confidence: number
  thesis: string
  evidence_refs: string[]
  overrides: CodexDecisionOverride[]
}

export interface CodexDecisionProposalPayload {
  research_packet_id: string
  proposal_schema_version: 'codex-proposal-v1'
  decision_scope: {
    max_new_positions: number
    primary_position_count: number
  }
  selections: CodexDecisionSelection[]
  portfolio_rationale: string
  no_action_reason?: string | null
  prompt_version: string
}

export interface CodexDecisionProposal {
  proposal_id: string
  research_packet_id: string
  proposal_schema_version: string
  prompt_version: string
  created_at: string
  status: string
  source: 'codex'
  payload: CodexDecisionProposalPayload
}

export interface DecisionValidationFailure extends DecisionConstraint {
  symbol?: string
}

export interface DecisionValidation {
  validation_id: string
  proposal_id: string
  research_packet_id: string
  validated_at: string
  status: DecisionValidationStatus
  hard_failures: DecisionValidationFailure[]
  accepted_overrides: Array<{
    symbol: string
    warning_code: string
    reason: string
    risk_adjustment: string
  }>
  recalculated: {
    total_cost: number
    total_planned_loss: number
    total_position_weight_pct: number
    selections: Array<{
      symbol: string
      action: DecisionAction
      requested_quantity: number
      total_cost: number
      planned_loss: number
      position_weight_pct: number
    }>
  }
  valid_until?: string | null
  trigger_time_revalidation_required: boolean
  validator_version: string
  disclaimer: string
}

export interface DecisionConfirmation {
  confirmation_id: string
  proposal_id: string
  validation_id: string
  accepted: boolean
  reason?: string | null
  confirmed_at: string
  execution_status: 'not_executed'
  disclaimer: string
}

export interface DecisionWorkspace {
  authority_mode: 'software_baseline' | 'codex_shadow' | 'codex_validated'
  authority: 'software_baseline' | 'codex_validated'
  is_final_decision: boolean
  requires_user_confirmation: boolean
  is_confirmed: boolean
  research_packet: DecisionResearchPacket
  software_baseline: SoftwareBaseline
  codex_proposal: CodexDecisionProposal | null
  validation: DecisionValidation | null
  confirmation: DecisionConfirmation | null
  primary_decision: CodexDecisionProposal | null
  disclaimer: string
}

export interface DecisionConfirmationInput {
  validation_id: string
  accepted: boolean
  reason?: string
}

export const decisionApi = {
  getResearch: (refresh = true) =>
    ApiClient.get<DecisionResearchPacket>('/api/decision/research/today', {
      refresh
    }),

  getBaseline: (refresh = false) =>
    ApiClient.get<SoftwareBaseline>('/api/decision/baseline/today', {
      refresh
    }),

  getFinal: (refresh = false) =>
    ApiClient.get<DecisionWorkspace>('/api/decision/final/today', {
      refresh
    }),

  revalidate: (proposalId: string, refreshQuote = true) =>
    ApiClient.post<DecisionValidation>(
      `/api/decision/proposals/${proposalId}/validate`,
      undefined,
      { params: { refresh_quote: refreshQuote } }
    ),

  confirm: (proposalId: string, payload: DecisionConfirmationInput) =>
    ApiClient.post<DecisionConfirmation>(
      `/api/decision/proposals/${proposalId}/confirm`,
      payload
    )
}
