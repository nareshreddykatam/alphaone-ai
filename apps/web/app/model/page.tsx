"use client"

import { useState, useEffect } from "react"
import { Navigation } from "@/components/Navigation"
import { DashboardCard } from "@/components/DashboardCard"

const API = process.env.NEXT_PUBLIC_API_URL

export default function ModelPage() {
  const [data, setData] = useState<any>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetch(`${API}/api/v1/model/`)
      .then((r) => r.json())
      .then(setData)
      .catch((e) => console.error("Failed to fetch model info:", e))
      .finally(() => setLoading(false))
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
      </main>
    </div>
  )
}
