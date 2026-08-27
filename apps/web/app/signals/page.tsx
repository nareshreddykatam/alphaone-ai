"use client"

import { useState, useEffect } from "react"
import { Navigation } from "@/components/Navigation"
import { parseUtcDate } from "@/lib/time"
import { formatINR } from "@/lib/currency"

const QUALITY_STYLES: Record<string, string> = {
  HIGH: "text-long",
  MEDIUM: "text-no-trade",
  LOW: "text-muted-foreground",
}

export default function SignalsPage() {
  const [signals, setSignals] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [generating, setGenerating] = useState(false)

  const fetchSignals = async () => {
    try {
      const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL}/api/v1/signals/?limit=50`)
      const json = await res.json()
      setSignals(json.signals || [])
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

  return (
    <div className="min-h-screen">
      <Navigation currentPage="signals" />
      <main className="container mx-auto px-4 py-6">
        <div className="mb-6 flex items-center justify-between flex-wrap gap-2">
          <div>
            <h1 className="text-2xl font-bold">Signal History</h1>
            <p className="text-muted-foreground text-sm">
              Research signals -- LOW/MEDIUM/HIGH quality, not a validated accuracy claim.
            </p>
            {signals[0]?.conversion_source && (
              <p className="text-muted-foreground text-xs mt-1">
                Prices shown in INR via {signals[0].conversion_source} ({signals[0].conversion_status})
              </p>
            )}
          </div>
          <button
            onClick={generateSignal}
            disabled={generating}
            className="px-4 py-2 rounded-lg bg-primary text-black text-sm font-semibold disabled:opacity-50"
          >
            {generating ? "Checking..." : "Check Latest Signal"}
          </button>
        </div>

        <div className="dashboard-card overflow-x-auto">
          {loading ? (
            <div className="h-24 bg-muted rounded animate-pulse" />
          ) : signals.length === 0 ? (
            <p className="text-muted-foreground text-sm py-8 text-center">No signals yet.</p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-muted-foreground border-b border-border">
                  <th className="py-2 pr-4">Time</th>
                  <th className="py-2 pr-4">Type</th>
                  <th className="py-2 pr-4">Quality</th>
                  <th className="py-2 pr-4">Entry</th>
                  <th className="py-2 pr-4">SL</th>
                  <th className="py-2 pr-4">TP1</th>
                  <th className="py-2 pr-4">R:R</th>
                  <th className="py-2 pr-4">Regime</th>
                  <th className="py-2 pr-4">Strategy</th>
                  <th className="py-2">Reasoning</th>
                </tr>
              </thead>
              <tbody>
                {signals.map((s) => (
                  <tr key={s.signal_id} className="border-b border-border/50">
                    <td className="py-2 pr-4 font-mono text-xs whitespace-nowrap">
                      {s.timestamp ? parseUtcDate(s.timestamp).toLocaleString() : "--"}
                    </td>
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
                    <td className="py-2 pr-4 font-mono">{s.conversion_status && s.conversion_status !== "UNAVAILABLE" ? formatINR(s.entry_price_inr) : "N/A"}</td>
                    <td className="py-2 pr-4 font-mono text-short">{s.conversion_status && s.conversion_status !== "UNAVAILABLE" ? formatINR(s.stop_loss_inr) : "N/A"}</td>
                    <td className="py-2 pr-4 font-mono text-long">{s.conversion_status && s.conversion_status !== "UNAVAILABLE" ? formatINR(s.take_profit_1_inr) : "N/A"}</td>
                    <td className="py-2 pr-4 font-mono">{s.risk_reward ? `1:${s.risk_reward}` : "--"}</td>
                    <td className="py-2 pr-4 font-mono text-xs">{s.market_regime || "--"}</td>
                    <td className="py-2 pr-4 text-xs">{s.strategy_name || "--"}</td>
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
