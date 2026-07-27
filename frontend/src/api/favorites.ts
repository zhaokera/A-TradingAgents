import { ApiClient } from './request'

export interface FavoriteItem {
  symbol?: string  // 主字段：6位股票代码
  stock_code?: string  // 兼容字段（已废弃）
  stock_name: string
  market: string
  board?: string
  exchange?: string
  added_at?: string
  tags?: string[]
  notes?: string
  alert_price_high?: number | null
  alert_price_low?: number | null
  current_price?: number | null
  change_percent?: number | null
  volume?: number | null
  quote_source?: string | null
  quote_trade_at?: string | null
  quote_checked_at?: string | null
  source?: 'manual' | 'ai_screening'
  lifecycle_state?: 'manual' | 'current' | 'superseded' | 'expired' | 'invalidated' | 'target_reached'
  is_current_ai_candidate?: boolean
  ai_metadata?: {
    run_id?: string
    generated_at?: string
    reason_summary?: string
    reference_price?: number | null
    price_plan?: {
      observation_zone?: number[] | null
      entry_price?: number | null
      breakout_price?: number | null
      stop_price?: number | null
      target_price?: number | null
      entry_strategy?: 'pullback' | 'breakout' | 'reference'
      entry_status?: string
      entry_status_label?: string
      entry_guidance?: string
      status?: string
    }
    objective_id?: string
    objective_label?: string
    objective_tier?: 'core' | 'related' | 'non_core'
    objective_tier_label?: string
    objective_segment?: string
    horizon?: string
    source?: string
    is_reference_only?: boolean
    tracking_enabled?: boolean
    actionability?: string
    actionability_label?: string
    rank_score?: number
    last_checked_at?: string
    quote_source?: string
    quote_trade_at?: string
    position_sizing?: {
      status?: string
      suggested_quantity?: number
      suggested_amount?: number
      suggested_position_pct?: number
      planned_loss_amount?: number
    }
    performance?: {
      return_since_generated_pct?: number
      max_return_pct?: number
      min_return_pct?: number
      observation_count?: number
      target_hit_at?: string
      stop_hit_at?: string
    }
    lifecycle_state?: 'current' | 'superseded' | 'expired' | 'invalidated' | 'target_reached'
    is_current?: boolean
    superseded_at?: string | null
    superseded_by_run_id?: string | null
  } | null
}

export interface AddFavoriteReq {
  symbol?: string  // 主字段：6位股票代码
  stock_code?: string  // 兼容字段（已废弃）
  stock_name: string
  market?: string
  tags?: string[]
  notes?: string
  alert_price_high?: number | null
  alert_price_low?: number | null
}

export const favoritesApi = {
  /**
   * 获取收藏列表
   */
  list: () => ApiClient.get<FavoriteItem[]>('/api/favorites/'),

  /**
   * 添加收藏
   * @param payload 收藏信息（需包含 symbol 或 stock_code）
   */
  add: (payload: AddFavoriteReq) => ApiClient.post<{ message: string; symbol?: string; stock_code?: string }>('/api/favorites/', payload),

  /**
   * 更新收藏
   * @param symbol 股票代码（6位）
   * @param payload 更新内容
   */
  update: (symbol: string, payload: Partial<Pick<FavoriteItem, 'tags' | 'notes' | 'alert_price_high' | 'alert_price_low'>>) =>
    ApiClient.put<{ message: string; symbol?: string; stock_code?: string }>(`/api/favorites/${symbol}`, payload),

  /**
   * 删除收藏
   * @param symbol 股票代码（6位）
   */
  remove: (symbol: string) => ApiClient.delete<{ message: string; symbol?: string; stock_code?: string }>(`/api/favorites/${symbol}`),

  /**
   * 检查是否已收藏
   * @param symbol 股票代码（6位）
   */
  check: (symbol: string) => ApiClient.get<{ symbol?: string; stock_code?: string; is_favorite: boolean }>(`/api/favorites/check/${symbol}`),

  /**
   * 获取所有标签
   */
  tags: () => ApiClient.get<string[]>('/api/favorites/tags'),

  /**
   * 同步自选股实时行情
   * @param data_source 数据源（tushare/akshare）
   */
  syncRealtime: (data_source: string = 'tushare') =>
    ApiClient.post<{
      total: number
      success_count: number
      failed_count: number
      symbols: string[]
      data_source: string
      message: string
    }>('/api/favorites/sync-realtime', { data_source })
}
