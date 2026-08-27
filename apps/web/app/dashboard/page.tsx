"use client"

import { useState, useEffect } from "react"
import { Navigation } from "@/components/Navigation"
import { DashboardCard } from "@/components/DashboardCard"
import { SignalDisplay } from "@/components/SignalDisplay"
import { MarketStatusCard } from "@/components/MarketStatusCard"
import { parseUtcDate } from "@/lib/time"
import { formatINR } from "@/lib/currency"

const API = process.env.NEXT_PUBLIC_API_URL

const CONNECTION_STYLES: Record<string, string> = {
  LIVE: "border-long text-long",
  STALE: "border-no-trade text-no-trade",
  DISCONNECTED: "border-short text-short",
  NOT_CONFIGURED: "border-border text-muted-foreground",
}

const CONNECTION_LABELS: Record<string, string> = {
  LIVE: "Live",
  STALE: "Stale",
  DISCONNECTED: "Disconnected",
  NOT_CONFIGURED: "Not Configured",
}

function freshnessLabel(lastSyncedAt: string | null) {
  if (!lastSyncedAt) return null
  const seconds = Math.max(0, Math.floor((Date.now() - parseUtcDate(lastSyncedAt).getTime()) / 1000))
  if (seconds < 60) return `Updated ${seconds}s ago`
  if (seconds < 3600) return `Updated ${Math.floor(seconds / 60)}m ago`
  return `Updated ${Math.floor(seconds / 3600)}h ago`
}

export default function DashboardPage() {
  const [data, setData] = useState<any>(null)
  const [balance, setBalance] = useState<any>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const fetchData = async () => {
      try {
        const [dashRes, balRes] = await Promise.all([
          fetch(`${API}/api/v1/dashboard/`),
          fetch(`${API}/api/v1/accounts/balance`),
        ])
        setData(await dashRes.json())
        setBalance(await balRes.json())
      } catch (e) {
        console.error("Failed to fetch dashboard:", e)
      } finally {
        setLoading(false)
      }
    }
    fetchData()
    const interval = setInterval(fetchData, 10000)
    return () => clearInterval(interval)
  }, [])

  const connState = data?.account_data_source || "NOT_CONFIGURED"
  const freshness = freshnessLabel(data?.last_synced_at)

  return (
    <div className="min-h-screen">
      <Navigation currentPage="dashboard" />
      <main className="container mx-auto px-4 py-6">
        <div className="mb-6 flex items-center justify-between flex-wrap gap-2">
          <div>
            <h1 className="text-2xl font-bold">Dashboard</h1>
            <p className="text-muted-foreground text-sm">
              BTC/USDT Perpetual Futures &middot; Manual Execution on CoinDCX
            </p>
          </div>
          <div className={`px-3 py-1 rounded-lg text-xs font-mono border ${CONNECTION_STYLES[connState] || ""}`}>
            COINDCX: {CONNECTION_LABELS[connState] || connState}
            {freshness && <span className="ml-2 text-muted-foreground">&middot; {freshness}</span>}
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
          <DashboardCard title="Total Equity" loading={loading}
            value={formatINR(balance?.total_equity)} />
          <DashboardCard title="Available Balance" loading={loading}
            value={formatINR(balance?.available_balance)} />
          <DashboardCard title="Used Margin" loading={loading}
            value={formatINR(balance?.used_margin)} />
          <DashboardCard title="Open Positions" loading={loading} value={data?.open_positions ?? "0"} />
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-6">
          <MarketStatusCard
            priceInr={data?.btc_price_inr}
            markPriceUsdt={data?.market_data_mark_price_usdt}
            status={data?.market_data_status}
            source={data?.market_data_source}
            updatedAt={data?.btc_price_updated_at}
            conversionStatus={data?.conversion_status}
            conversionRateUsdtInr={data?.conversion_rate}
          />
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
          <DashboardCard
            title="Current Signal"
            value={data?.current_signal || "NO_TRADE"}
            loading={loading}
            className={
              data?.current_signal === "LONG"
                ? "text-long"
                : data?.current_signal === "SHORT"
                ? "text-short"
                : "text-no-trade"
            }
          />
          <DashboardCard
            title="Signal Quality"
            value={data?.signal_quality || "--"}
            loading={loading}
          />
          <DashboardCard
            title="Risk Status"
            value={data?.risk_status || "--"}
            loading={loading}
            className={data?.risk_status === "HARD_KILL" ? "text-short" : data?.risk_status === "ACTIVE" ? "text-long" : "text-no-trade"}
          />
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
          <DashboardCard
            title="Unrealized P&L"
            value={formatINR(data?.unrealized_pnl, { showSign: true })}
            loading={loading}
            className={data?.unrealized_pnl > 0 ? "text-long" : data?.unrealized_pnl < 0 ? "text-short" : ""}
          />
          <DashboardCard
            title="Today's P&L"
            value={formatINR(data?.daily_pnl, { showSign: true })}
            loading={loading}
            className={data?.daily_pnl > 0 ? "text-long" : data?.daily_pnl < 0 ? "text-short" : ""}
          />
          <DashboardCard
            title="Trading Mode"
            value={data?.trading_mode || "--"}
            loading={loading}
          />
          <DashboardCard
            title="Telegram"
            value={data?.telegram_enabled ? "Enabled" : "Disabled"}
            loading={loading}
          />
        </div>

        <SignalDisplay
          signal={data?.current_signal}
          regime={data?.market_regime}
          quality={data?.signal_quality}
          entryInr={data?.signal_entry_price_inr}
          slInr={data?.signal_stop_loss_inr}
          tp1Inr={data?.signal_take_profit_1_inr}
          rr={data?.signal_risk_reward}
        />
      </main>
    </div>
  )
}
