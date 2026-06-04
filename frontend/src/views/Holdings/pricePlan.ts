export type PricePlanKey = 'stop' | 'target' | 'sell' | 'buy'
export type PricePlanTone = 'danger' | 'success' | 'warning' | 'info'
export type PricePlanSource = 'manual' | 'report' | 'none'

export interface PricePlanInput {
  currentPrice?: number | null
  manualStopLossPrice?: number | null
  manualTargetPrice?: number | null
  manualSellPrice?: number | null
  manualBuyPrice?: number | null
  reportStopLossPrice?: number | null
  reportTargetPrice?: number | null
  reportSellPrice?: number | null
  reportBuyPrice?: number | null
}

export interface PricePlanRow {
  key: PricePlanKey
  label: string
  tone: PricePlanTone
  manualPrice: number | null
  reportPrice: number | null
  activePrice: number | null
  activeSource: PricePlanSource
  distancePct: number | null
}

const normalizePrice = (value: number | null | undefined): number | null => {
  if (value === null || value === undefined) return null
  const normalized = Number(value)
  if (!Number.isFinite(normalized) || normalized <= 0) return null
  return normalized
}

const buildRow = (
  key: PricePlanKey,
  label: string,
  tone: PricePlanTone,
  manual: number | null | undefined,
  report: number | null | undefined,
  currentPrice: number | null
): PricePlanRow => {
  const manualPrice = normalizePrice(manual)
  const reportPrice = normalizePrice(report)
  const activePrice = manualPrice ?? reportPrice
  const activeSource: PricePlanSource = manualPrice !== null ? 'manual' : reportPrice !== null ? 'report' : 'none'
  const distancePct = activePrice !== null && currentPrice && currentPrice > 0
    ? (activePrice - currentPrice) / currentPrice * 100
    : null

  return {
    key,
    label,
    tone,
    manualPrice,
    reportPrice,
    activePrice,
    activeSource,
    distancePct
  }
}

export const buildPricePlanRows = (input: PricePlanInput): PricePlanRow[] => {
  const currentPrice = normalizePrice(input.currentPrice)
  return [
    buildRow('stop', '止损', 'danger', input.manualStopLossPrice, input.reportStopLossPrice, currentPrice),
    buildRow('target', '目标', 'success', input.manualTargetPrice, input.reportTargetPrice, currentPrice),
    buildRow('sell', '卖出', 'warning', input.manualSellPrice, input.reportSellPrice, currentPrice),
    buildRow('buy', '追入', 'info', input.manualBuyPrice, input.reportBuyPrice, currentPrice)
  ]
}
