"use client"

import { useState, useEffect } from "react"
import { Navigation } from "@/components/Navigation"
import { DashboardCard } from "@/components/DashboardCard"

const API = process.env.NEXT_PUBLIC_API_URL

export default function ModelPage() {
  const [data, setData] = useState<any>(null)
  const [aiStatus, setAiStatus] = useState<any>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetch(`${API}/api/v1/model/`)
      .then((r) => r.json())
      .then(setData)
      .catch((e) => console.error("Failed to fetch model info:", e))
      .finally(() => setLoading(false))

    fetch(`${API}/api/v1/model/ai-status`)
      .then((r) => r.json())
      .then(setAiStatus)
      .catch((e) => console.error("Failed to fetch AI status:", e))
  }, [])

  return (
    <div className="min-h-screen">
      <Navigation currentPage="model" />
      <main className="container mx-auto px-4 py-6">
        <div className="mb-6">
          <h1 className="text-2xl font-bold">Model</h1>
          <p className="text-muted-foreground text-sm">Phase 3 ML research status.</p>
        </div>

        {!loading && data?.status === "NO_MODEL_DEPLOYED" ? (
          <div className="dashboard-card border-no-trade">
            <p className="font-semibold text-no-trade mb-2">No ML model is deployed.</p>
            <p className="text-sm text-muted-foreground">{data.note}</p>
          </div>
        ) : (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <DashboardCard title="Model Version" loading={loading} value={data?.model_version || "--"} />
            <DashboardCard title="Training Period" loading={loading} value={data?.training_period || "--"} />
            <DashboardCard title="Status" loading={loading} value={data?.status || "--"} />
          </div>
        )}

        <div className="mt-8 mb-4">
          <h2 className="text-xl font-bold">AI Trading V1 -- Orchestrator &amp; Paper Trading</h2>
          <p className="text-muted-foreground text-sm">
            Every AI paper trade is simulated only -- see reports/AI_TRADING_RESEARCH_V1.txt for the full research
            behind this. Automatic real-money trading remains disabled.
          </p>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <DashboardCard title="Model Health" loading={!aiStatus} value={aiStatus?.model_health?.status || "--"} />
          <DashboardCard
            title="Paper Equity"
            loading={!aiStatus}
            value={aiStatus ? `$${aiStatus.paper_trading.equity.toLocaleString()}` : "--"}
          />
          <DashboardCard
            title="Paper Total P&L"
            loading={!aiStatus}
            value={aiStatus ? `${aiStatus.paper_trading.total_pnl_pct.toFixed(2)}%` : "--"}
          />
          <DashboardCard
            title="Paper Trades Closed"
            loading={!aiStatus}
            value={aiStatus ? String(aiStatus.paper_trading.closed_trades) : "--"}
          />
        </div>
        {aiStatus?.model_health?.reasons?.length > 0 && (
          <div className="dashboard-card border-no-trade mt-4">
            <p className="font-semibold text-no-trade mb-2">Model health notes</p>
            <ul className="text-sm text-muted-foreground list-disc pl-5">
              {aiStatus.model_health.reasons.map((r: string, i: number) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          </div>
        )}
      </main>
    </div>
  )
}
