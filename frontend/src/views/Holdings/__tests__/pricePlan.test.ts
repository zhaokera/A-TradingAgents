import { describe, expect, it } from 'vitest'
import { buildPricePlanRows } from '../pricePlan'

describe('buildPricePlanRows', () => {
  it('prefers manual prices and keeps report prices for comparison', () => {
    const rows = buildPricePlanRows({
      currentPrice: 63.36,
      manualStopLossPrice: 58.8,
      manualTargetPrice: null,
      manualSellPrice: 66,
      manualBuyPrice: undefined,
      reportStopLossPrice: 57.5,
      reportTargetPrice: 70.4,
      reportSellPrice: 65.2,
      reportBuyPrice: 61.8
    })

    expect(rows[0]).toMatchObject({
      key: 'stop',
      label: '止损',
      activeSource: 'manual',
      activePrice: 58.8,
      reportPrice: 57.5,
      distancePct: expect.closeTo(-7.2, 1)
    })
    expect(rows[1]).toMatchObject({
      key: 'target',
      activeSource: 'report',
      activePrice: 70.4,
      reportPrice: 70.4,
      distancePct: expect.closeTo(11.11, 2)
    })
    expect(rows[2]).toMatchObject({
      key: 'sell',
      activeSource: 'manual',
      activePrice: 66,
      reportPrice: 65.2
    })
    expect(rows[3]).toMatchObject({
      key: 'buy',
      activeSource: 'report',
      activePrice: 61.8,
      reportPrice: 61.8
    })
  })
})
