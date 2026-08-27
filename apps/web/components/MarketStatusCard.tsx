"use client"

import { useEffect, useState } from "react"
import { formatINR, conversionStatusLabel } from "@/lib/currency"
import { parseUtcDate } from "@/lib/time"

interface MarketStatusCardProps {
  priceInr: number | null | undefined
  markPriceUsdt: number | null | undefined
  status: string | null | undefined // LIVE / STALE / DISCONNECTED / UNAVAILABLE
  source: string | null | undefined // "CoinDCX WebSocket" | "Binance (historical candle ingestion)"
  updatedAt: string | null | undefined // ISO timestamp
  conversionStatus: string | null | undefined
  conversionRateUsdtInr: number | null | undefined
}

const STATUS_CONFIG: Record<string, { icon: string; label: string; className: string }> = {
  LIVE: { icon: "●", label: "LIVE", className: "text-long" }, // ●
  STALE: { icon: "⚠", label: "STALE", className: "text-no-trade" }, // ⚠
  DISCONNECTED: { icon: "🔴", label: "DISCONNECTED", className: "text-short" }, // 🔴
  UNAVAILABLE: { icon: "", label: "Market data unavailable", className: "text-muted-foreground" },
}

function freshnessText(updatedAt: string | null | undefined, now: number): string | null {
  if (!updatedAt) return null
  const seconds = Math.max(0, (now - parseUtcDate(updatedAt).getTime()) / 1000)
  if (seconds < 10) return `Updated ${seconds.toFixed(1)}s ago`
  if (seconds < 60) return `Updated ${Math.floor(seconds)}s ago`
  if (seconds < 3600) return `Updated ${Math.floor(seconds / 60)}m ago`
  return `Updated ${Math.floor(seconds / 3600)}h ago`
}

export function MarketStatusCard({
  priceInr, markPriceUsdt, status, source, updatedAt, conversionStatus, conversionRateUsdtInr,
}: MarketStatusCardProps) {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    const tick = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(tick)
  }, [])

  const config = STATUS_CONFIG[status || "UNAVAILABLE"] || STATUS_CONFIG.UNAVAILABLE
  const priceText = conversionStatus && conversionStatus !== "UNAVAILABLE"
    ? formatINR(priceInr)
    : conversionStatusLabel(conversionStatus)
  const markPriceInr = markPriceUsdt != null && conversionRateUsdtInr != null
    ? markPriceUsdt * conversionRateUsdtInr
    : null
  const freshness = status === "UNAVAILABLE" ? null : freshnessText(updatedAt, now)

  return (
    <div className="dashboard-card">
      <div className="flex items-center justify-between mb-3">
        <p className="stat-label">BTC/USDT PERPETUAL</p>
        <div className={`flex items-center gap-1.5 text-xs font-mono font-semibold ${config.className}`}>
          {config.icon && <span>{config.icon}</span>}
          <span>{config.label}</span>
        </div>
      </div>

      <p className="text-2xl font-bold font-mono mb-3">{priceText}</p>

      <div className="grid grid-cols-2 gap-3 text-xs">
        <div>
          <p className="text-muted-foreground">Source</p>
          <p className="font-mono">{source || "--"}</p>
        </div>
        <div>
          <p className="text-muted-foreground">Updated</p>
          <p className="font-mono">{freshness || "--"}</p>
        </div>
        {markPriceInr != null && (
          <div className="col-span-2">
            <p className="text-muted-foreground">Mark Price</p>
            <p className="font-mono">{formatINR(markPriceInr)}</p>
          </div>
        )}
      </div>
    </div>
  )
}
