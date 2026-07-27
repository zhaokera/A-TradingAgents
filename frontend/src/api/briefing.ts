import { ApiClient } from './request'
import type { AICandidateItem } from './screening'

export interface DailyBriefing {
  as_of: string
  account: { total_assets?: number; available_cash?: number; current_exposure_pct?: number }
  holdings: { count: number; market_value: number; unrealized_pnl: number; items: any[] }
  market: {
    domestic_regime?: string
    combined_regime?: string
    macro_risk?: { status?: string; regime?: string; score?: number; factors?: any[] }
  }
  candidate_run: {
    run_id?: string
    generated_at?: string
    candidate_count: number
    executable_count: number
    executable_candidates: AICandidateItem[]
    portfolio_plan?: Record<string, any>
  }
  favorites: { count: number; lifecycle_counts: Record<string, number> }
  notifications: { unread_count: number }
}

export const briefingApi = {
  today: (refresh = false) =>
    ApiClient.get<DailyBriefing>(`/api/briefing/today?refresh=${refresh}`)
}
