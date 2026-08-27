"use client"

import { useState, useEffect } from "react"
import { Navigation } from "@/components/Navigation"
import { DashboardCard } from "@/components/DashboardCard"
import { formatINR } from "@/lib/currency"

const API = process.env.NEXT_PUBLIC_API_URL

function pct(v: number | null | undefined, digits = 1) {
  return v != null ? `${(v * 100).toFixed(digits)}%` : "N/A"
}

export default function PerformancePage() {
  const [data, setData] = useState<any>(null)
  const [missed, setMissed] = useState<any>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    Promise.all([
      fetch(`${API}/api/v1/portfolio/performance`).then((r) => r.json()),
      fetch(`${API}/api/v1/portfolio/missed-signals`).then((r) => r.json()),
    ])
      .then(([perf, ms]) => {
        setData(perf)
        setMissed(ms)
      })
      .catch((e) => console.error("Failed to fetch performance:", e))
      .finally(() => setLoading(false))
  }, [])

  const userActual = data?.user_actual
  const alphaOne = data?.alphaone_signals
  const backtest = data?.backtest

  return (
    <div className="min-h-screen">
      <Navigation currentPage="performance" />
      <main className="container mx-auto px-4 py-6 space-y-8">
        <div>
          <h1 className="text-2xl font-bold">Performance</h1>
          <p className="text-muted-foreground text-sm">
            Three separate views -- never combined into one number.
          </p>
        </div>

        <section>
          <h2 className="text-lg font-semibold mb-3">Your Actual Trading (CoinDCX, manually executed)</h2>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <DashboardCard title="Total P&L" loading={loading}
              value={formatINR(userActual?.total_pnl, { showSign: true })}
              className={userActual?.total_pnl > 0 ? "text-long" : userActual?.total_pnl < 0 ? "text-short" : ""} />
            <DashboardCard title="Trades" loading={loading} value={userActual?.total_trades ?? "0"} />
            <DashboardCard title="Win Rate" loading={loading} value={pct(userActual?.win_rate)} />
            <DashboardCard title="Profit Factor" loading={loading} value={userActual?.profit_factor?.toFixed(2) ?? "N/A"} />
          </div>
        </section>

        <section>
          <h2 className="text-lg font-semibold mb-3">AlphaOne Signal Performance (hypothetical -- following every signal)</h2>
          <p className="text-xs text-muted-foreground mb-3">
            What every generated signal would have earned, whether or not you took it. Not your real results.
          </p>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <DashboardCard title="Total Signals" loading={loading} value={alphaOne?.total_signals ?? "0"} />
            <DashboardCard title="Resolved" loading={loading} value={alphaOne?.resolved_signals ?? "0"} />
            <DashboardCard title="Win Rate" loading={loading} value={pct(alphaOne?.win_rate)} />
            <DashboardCard title="No-Trade Rate" loading={loading} value={pct(alphaOne?.no_trade_rate)} />
          </div>
          {missed && (
            <div className="dashboard-card mt-4 text-sm">
              <p className="stat-label mb-2">All Signals vs. Taken vs. Missed</p>
              <div className="grid grid-cols-3 gap-4 font-mono">
                <div><p className="text-muted-foreground text-xs">All</p><p>{missed.all_signals.count}</p></div>
                <div><p className="text-muted-foreground text-xs">Taken by you</p><p>{missed.user_taken.count}</p></div>
                <div><p className="text-muted-foreground text-xs">Missed</p><p>{missed.missed.count}</p></div>
              </div>
            </div>
          )}
        </section>

        <section>
          <h2 className="text-lg font-semibold mb-3">Backtest Research (Phase 2/3 historical, not live)</h2>
          {backtest ? (
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <DashboardCard title="Strategy" loading={loading} value={backtest.strategy_name} />
              <DashboardCard title="Total Return" loading={loading} value={`${backtest.total_pnl_pct?.toFixed(2)}%`} />
              {/* backtest.win_rate is stored 0-100 (Phase 2 BacktestMetric convention),
                  unlike user_actual/alphaone_signals' 0-1 fractions -- do not run through pct() */}
              <DashboardCard title="Win Rate" loading={loading} value={backtest.win_rate != null ? `${backtest.win_rate.toFixed(1)}%` : "N/A"} />
              <DashboardCard title="Max Drawdown" loading={loading} value={`${backtest.max_drawdown_pct?.toFixed(2)}%`} />
            </div>
          ) : (
            <p className="text-muted-foreground text-sm">No backtest runs recorded yet.</p>
          )}
        </section>
      </main>
    </div>
  )
}
