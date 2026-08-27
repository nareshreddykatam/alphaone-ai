"use client"

import { useState, useEffect } from "react"
import { Navigation } from "@/components/Navigation"
import { DashboardCard } from "@/components/DashboardCard"
import { parseUtcDate } from "@/lib/time"
import { formatINR } from "@/lib/currency"

const API = process.env.NEXT_PUBLIC_API_URL

const STATUS_STYLES: Record<string, string> = {
  ACTIVE: "text-long border-long",
  DAILY_LIMIT: "text-no-trade border-no-trade",
  COOLDOWN: "text-no-trade border-no-trade",
  HARD_KILL: "text-short border-short",
}

export default function RiskPage() {
  const [data, setData] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [resetting, setResetting] = useState(false)

  const fetchStatus = async () => {
    try {
      const res = await fetch(`${API}/api/v1/risk/`)
      setData(await res.json())
    } catch (e) {
      console.error("Failed to fetch risk status:", e)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchStatus()
  }, [])

  const resetHardKill = async () => {
    setResetting(true)
    try {
      await fetch(`${API}/api/v1/risk/reset-hard-kill`, { method: "POST" })
      await fetchStatus()
    } finally {
      setResetting(false)
    }
  }

  const status = data?.risk_status

  return (
    <div className="min-h-screen">
      <Navigation currentPage="risk" />
      <main className="container mx-auto px-4 py-6">
        <div className="mb-6 flex items-center justify-between flex-wrap gap-2">
          <div>
            <h1 className="text-2xl font-bold">Risk Status</h1>
            <p className="text-muted-foreground text-sm">
              Informational only -- AlphaOne cannot place, block, or modify trades.
            </p>
          </div>
          {status && (
            <div className={`px-4 py-2 rounded-lg border font-mono text-sm ${STATUS_STYLES[status] || ""}`}>
              {status}
            </div>
          )}
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
          <DashboardCard title="Risk Per Trade" loading={loading} value={data ? `${data.risk_per_trade_pct}%` : "--"} />
          <DashboardCard title="Max Daily Loss" loading={loading} value={data ? `${data.max_daily_loss_pct}%` : "--"} />
          <DashboardCard title="Max Drawdown" loading={loading} value={data ? `${data.max_drawdown_pct}%` : "--"} />
          <DashboardCard title="Current Drawdown" loading={loading}
            value={data ? `${data.current_drawdown_pct}%` : "--"}
            className={data?.current_drawdown_pct > 0 ? "text-short" : ""} />
          <DashboardCard title="Today's P&L" loading={loading} value={data ? `${data.current_daily_pnl_pct}%` : "--"}
            className={data?.current_daily_pnl_pct < 0 ? "text-short" : "text-long"} />
          <DashboardCard title="Trades Today" loading={loading} value={data ? `${data.trades_today} / ${data.max_daily_trades}` : "--"} />
          <DashboardCard title="Consecutive Losses" loading={loading} value={data?.consecutive_losses ?? "--"} />
          <DashboardCard title="Current Equity" loading={loading} value={data ? formatINR(data.current_equity) : "--"} />
        </div>

        {status === "HARD_KILL" && (
          <div className="dashboard-card border-short">
            <p className="text-sm text-short mb-3">
              Max drawdown breached. This never auto-resets -- confirm you understand your current
              drawdown before resuming.
            </p>
            <button
              onClick={resetHardKill}
              disabled={resetting}
              className="px-4 py-2 rounded-lg bg-short text-white text-sm font-semibold disabled:opacity-50"
            >
              {resetting ? "Resetting..." : "Manually Reset Hard Kill"}
            </button>
          </div>
        )}

        {status === "COOLDOWN" && data?.cooldown_until && (
          <div className="dashboard-card border-no-trade text-sm text-no-trade">
            Cooldown after {data.consecutive_losses} consecutive losses -- resumes automatically at{" "}
            {parseUtcDate(data.cooldown_until).toLocaleString()}.
          </div>
        )}
      </main>
    </div>
  )
}
