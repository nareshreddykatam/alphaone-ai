"use client"

import { useState, useEffect, useMemo } from "react"
import { Navigation } from "@/components/Navigation"
import { parseUtcDate } from "@/lib/time"
import { formatINR, formatUSDT } from "@/lib/currency"

const QUALITY_STYLES: Record<string, string> = {
  HIGH: "text-long",
  MEDIUM: "text-no-trade",
  LOW: "text-muted-foreground",
}

// USDT is the actual BTC/USDT Perpetual trading level (primary); INR is
// the secondary converted representation, shown only when available.
function PriceCell({ usdt, inr, hasConversion, className }: { usdt: number | null | undefined; inr: number | null | undefined; hasConversion: boolean; className?: string }) {
  return (
    <div className={className}>
      <p className="font-mono">{formatUSDT(usdt)}</p>
      {hasConversion && inr != null && <p className="font-mono text-xs text-muted-foreground">≈ {formatINR(inr)}</p>}
    </div>
  )
}

const ALL = "ALL"

export default function SignalsPage() {
  const [signals, setSignals] = useState<any[]>([])
  const [strategies, setStrategies] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [generating, setGenerating] = useState(false)

  const [strategyFilter, setStrategyFilter] = useState(ALL)
  const [timeframeFilter, setTimeframeFilter] = useState(ALL)
  const [typeFilter, setTypeFilter] = useState(ALL)
  const [qualityFilter, setQualityFilter] = useState(ALL)

  const fetchSignals = async () => {
    try {
      const [signalsRes, strategiesRes] = await Promise.all([
        fetch(`${process.env.NEXT_PUBLIC_API_URL}/api/v1/signals/?limit=50`),
        fetch(`${process.env.NEXT_PUBLIC_API_URL}/api/v1/signals/strategies`),
      ])
      const signalsJson = await signalsRes.json()
      const strategiesJson = await strategiesRes.json()
      setSignals(signalsJson.signals || [])
      setStrategies(strategiesJson.strategies || [])
    } catch (e) {
      console.error("Failed to fetch signals:", e)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchSignals()
  }, [])

  const generateSignal = async () => {
    setGenerating(true)
    try {
      await fetch(`${process.env.NEXT_PUBLIC_API_URL}/api/v1/signals/generate`, { method: "POST" })
      await fetchSignals()
    } catch (e) {
      console.error("Failed to generate signal:", e)
    } finally {
      setGenerating(false)
    }
  }

  const filteredSignals = useMemo(() => {
    return signals.filter((s) => {
      if (strategyFilter !== ALL && (s.strategy_id || s.strategy_name) !== strategyFilter) return false
      if (timeframeFilter !== ALL && s.timeframe !== timeframeFilter) return false
      if (typeFilter !== ALL && s.signal_type !== typeFilter) return false
      if (qualityFilter !== ALL && s.quality !== qualityFilter) return false
      return true
    })
  }, [signals, strategyFilter, timeframeFilter, typeFilter, qualityFilter])

  const timeframes = useMemo(() => Array.from(new Set(strategies.map((s) => s.timeframe))).sort(), [strategies])

  return (
    <div className="min-h-screen">
      <Navigation currentPage="signals" />
      <main className="container mx-auto px-4 py-6">
        <div className="mb-6 flex items-center justify-between flex-wrap gap-2">
          <div>
            <h1 className="text-2xl font-bold">Signal History</h1>
            <p className="text-muted-foreground text-sm">
              Research signals across {strategies.length || "multiple"} independent strategies -- LOW/MEDIUM/HIGH quality, not a validated accuracy claim.
              Only PRODUCTION_ELIGIBLE strategies ever generate a live signal; the rest exist for research only.
            </p>
            <p className="text-muted-foreground text-xs mt-1">
              Levels shown in USDT (actual CoinDCX trading price)
              {signals[0]?.conversion_source && ` with INR via ${signals[0].conversion_source} (${signals[0].conversion_status})`}
            </p>
          </div>
          <button
            onClick={generateSignal}
            disabled={generating}
            className="px-4 py-2 rounded-lg bg-primary text-black text-sm font-semibold disabled:opacity-50"
          >
            {generating ? "Checking..." : "Check Latest Signal"}
          </button>
        </div>

        <div className="mb-4 flex flex-wrap gap-3">
          <div>
            <label className="stat-label block mb-1">Strategy</label>
            <select
              className="bg-muted rounded px-2 py-1.5 text-sm"
              value={strategyFilter}
              onChange={(e) => setStrategyFilter(e.target.value)}
            >
              <option value={ALL}>All strategies</option>
              {strategies.map((s) => (
                <option key={s.strategy_id} value={s.strategy_id}>
                  {s.strategy_id} -- {s.display_name} ({
                    s.production_status === "PRODUCTION_ELIGIBLE" ? "live" : s.production_status === "REJECTED" ? "rejected" : "research"
                  })
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="stat-label block mb-1">Timeframe</label>
            <select
              className="bg-muted rounded px-2 py-1.5 text-sm"
              value={timeframeFilter}
              onChange={(e) => setTimeframeFilter(e.target.value)}
            >
              <option value={ALL}>All timeframes</option>
              {timeframes.map((tf) => (
                <option key={tf} value={tf}>{tf}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="stat-label block mb-1">Type</label>
            <select
              className="bg-muted rounded px-2 py-1.5 text-sm"
              value={typeFilter}
              onChange={(e) => setTypeFilter(e.target.value)}
            >
              <option value={ALL}>All types</option>
              <option value="LONG">LONG</option>
              <option value="SHORT">SHORT</option>
              <option value="NO_TRADE">NO_TRADE</option>
            </select>
          </div>
          <div>
            <label className="stat-label block mb-1">Quality</label>
            <select
              className="bg-muted rounded px-2 py-1.5 text-sm"
              value={qualityFilter}
              onChange={(e) => setQualityFilter(e.target.value)}
            >
              <option value={ALL}>All qualities</option>
              <option value="HIGH">HIGH</option>
              <option value="MEDIUM">MEDIUM</option>
              <option value="LOW">LOW</option>
            </select>
          </div>
        </div>

        <div className="dashboard-card overflow-x-auto">
          {loading ? (
            <div className="h-24 bg-muted rounded animate-pulse" />
          ) : filteredSignals.length === 0 ? (
            <p className="text-muted-foreground text-sm py-8 text-center">
              {signals.length === 0 ? "No signals yet." : "No signals match the current filters."}
            </p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-muted-foreground border-b border-border">
                  <th className="py-2 pr-4">Time</th>
                  <th className="py-2 pr-4">Strategy</th>
                  <th className="py-2 pr-4">Timeframe</th>
                  <th className="py-2 pr-4">Type</th>
                  <th className="py-2 pr-4">Quality</th>
                  <th className="py-2 pr-4">Entry</th>
                  <th className="py-2 pr-4">SL</th>
                  <th className="py-2 pr-4">TP1</th>
                  <th className="py-2 pr-4">TP2</th>
                  <th className="py-2 pr-4">TP3</th>
                  <th className="py-2 pr-4">R:R</th>
                  <th className="py-2 pr-4">Regime</th>
                  <th className="py-2">Reasoning</th>
                </tr>
              </thead>
              <tbody>
                {filteredSignals.map((s) => (
                  <tr key={s.signal_id} className="border-b border-border/50">
                    <td className="py-2 pr-4 font-mono text-xs whitespace-nowrap">
                      {s.timestamp ? parseUtcDate(s.timestamp).toLocaleString() : "--"}
                    </td>
                    <td className="py-2 pr-4 text-xs">
                      <span className="font-mono">{s.strategy_id || s.strategy_name || "--"}</span>
                      {s.strategy_display_name && s.strategy_display_name !== s.strategy_id && (
                        <span className="block text-muted-foreground">{s.strategy_display_name}</span>
                      )}
                    </td>
                    <td className="py-2 pr-4 font-mono text-xs">{s.timeframe || "--"}</td>
                    <td
                      className={`py-2 pr-4 font-bold ${
                        s.signal_type === "LONG" ? "text-long" : s.signal_type === "SHORT" ? "text-short" : "text-no-trade"
                      }`}
                    >
                      {s.signal_type}
                    </td>
                    <td className={`py-2 pr-4 font-mono ${s.quality ? QUALITY_STYLES[s.quality] : ""}`}>
                      {s.quality || "--"}
                    </td>
                    {(() => {
                      const hasConversion = !!s.conversion_status && s.conversion_status !== "UNAVAILABLE"
                      return (
                        <>
                          <td className="py-2 pr-4"><PriceCell usdt={s.entry_price} inr={s.entry_price_inr} hasConversion={hasConversion} /></td>
                          <td className="py-2 pr-4"><PriceCell usdt={s.stop_loss} inr={s.stop_loss_inr} hasConversion={hasConversion} className="text-short" /></td>
                          <td className="py-2 pr-4"><PriceCell usdt={s.take_profit_1} inr={s.take_profit_1_inr} hasConversion={hasConversion} className="text-long" /></td>
                          <td className="py-2 pr-4"><PriceCell usdt={s.take_profit_2} inr={s.take_profit_2_inr} hasConversion={hasConversion} className="text-long" /></td>
                          <td className="py-2 pr-4"><PriceCell usdt={s.take_profit_3} inr={s.take_profit_3_inr} hasConversion={hasConversion} className="text-long" /></td>
                        </>
                      )
                    })()}
                    <td className="py-2 pr-4 font-mono">{s.risk_reward ? `1:${s.risk_reward}` : "--"}</td>
                    <td className="py-2 pr-4 font-mono text-xs">{s.market_regime || "--"}</td>
                    <td className="py-2 text-xs text-muted-foreground max-w-md">{s.reasoning}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </main>
    </div>
  )
}
