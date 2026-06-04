import { ApiClient } from './request'

export type HoldingAction = 'buy' | 'sell' | 'hold'

export interface HoldingAnalysis {
  status: string
  action: HoldingAction
  reason: string
  quantity: number
  cost_price: number
  current_price: number | null
  market_value: number | null
  profit_loss: number | null
  profit_loss_pct: number | null
  target_monthly_return_pct: number
  monthly_target_progress_pct: number
  remaining_days_in_month: number
  required_daily_return_pct: number
  suggested_ratio_text: string
  suggested_shares_text: string
  is_reference_only: boolean
}

export interface HoldingAIAdvice {
  action: HoldingAction
  confidence: number
  suggested_buy_price?: number | null
  suggested_sell_price?: number | null
  target_price?: number | null
  stop_loss_price?: number | null
  position_suggestion: string
  reason: string
  risks: string[]
  model_name: string
  provider: string
  generated_at: string
  based_on_report?: Record<string, any> | null
  is_reference_only: boolean
}

export interface HoldingItem {
  id: string
  code: string
  name: string
  market: string
  quantity: number
  cost_price: number
  target_monthly_return_pct: number
  stop_loss_pct: number
  take_profit_pct?: number | null
  strategy: string
  notes?: string
  current_price?: number | null
  created_at?: string
  updated_at?: string
  analysis: HoldingAnalysis
  ai_advice?: HoldingAIAdvice | null
}

export interface HoldingSettings {
  total_assets: number
  configured_total_assets?: number | null
  is_auto_total_assets: boolean
  updated_at?: string | null
}

export interface HoldingPayload {
  code: string
  name?: string
  market?: string
  quantity: number
  cost_price: number
  target_monthly_return_pct: number
  stop_loss_pct: number
  take_profit_pct?: number | null
  strategy?: string
  notes?: string
}

export const holdingsApi = {
  list: () => ApiClient.get<{ items: HoldingItem[]; settings?: HoldingSettings }>('/api/holdings/'),
  updateSettings: (payload: { total_assets: number | null }) =>
    ApiClient.patch<{ settings: HoldingSettings }>('/api/holdings/settings', payload, { showLoading: true }),
  create: (payload: HoldingPayload) =>
    ApiClient.post<{ item: HoldingItem }>('/api/holdings/', payload, { showLoading: true }),
  update: (id: string, payload: Partial<HoldingPayload>) =>
    ApiClient.put<{ item: HoldingItem }>(`/api/holdings/${id}`, payload, { showLoading: true }),
  remove: (id: string) =>
    ApiClient.delete<{ id: string }>(`/api/holdings/${id}`),
  analyze: (id: string) =>
    ApiClient.post<{ item: HoldingItem }>(`/api/holdings/${id}/analyze`),
  aiAdvice: (id: string) =>
    ApiClient.post<{ item: HoldingItem; advice: HoldingAIAdvice }>(`/api/holdings/${id}/ai-advice`, undefined, { showLoading: true })
}
