"use client"

import { useState, useEffect, useRef } from "react"
import { Navigation } from "@/components/Navigation"
import { formatINR, conversionStatusLabel } from "@/lib/currency"

const API = process.env.NEXT_PUBLIC_API_URL
const TIMEFRAMES = ["15m", "1h", "4h", "1d"]

export default function ChartPage() {
  const containerRef = useRef<HTMLDivElement>(null)
  const [timeframe, setTimeframe] = useState("4h")
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [conversionStatus, setConversionStatus] = useState<string | null>(null)
  const [conversionSource, setConversionSource] = useState<string | null>(null)

  useEffect(() => {
    let chart: any
    let cancelled = false

    const build = async () => {
      setLoading(true)
      setError(null)
      try {
        const { createChart, ColorType } = await import("lightweight-charts")
        const res = await fetch(`${API}/api/v1/market/candles?symbol=BTC/USDT&timeframe=${timeframe}&limit=300`)
        const json = await res.json()
        if (cancelled || !containerRef.current) return

        setConversionStatus(json.conversion_status ?? null)
        setConversionSource(json.conversion_source ?? null)

        if (!json.candles || json.candles.length === 0) {
          setError("No candle data ingested yet for this symbol/timeframe.")
          setLoading(false)
          return
        }

        if (json.conversion_status === "UNAVAILABLE" || json.candles.some((c: any) => c.close_inr == null)) {
          setError(conversionStatusLabel(json.conversion_status))
          setLoading(false)
          return
        }

        containerRef.current.innerHTML = ""
        chart = createChart(containerRef.current, {
          layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: "#a1a1aa" },
          grid: { vertLines: { color: "#1e1e2e" }, horzLines: { color: "#1e1e2e" } },
          width: containerRef.current.clientWidth,
          height: 480,
          timeScale: { timeVisible: true },
          localization: { priceFormatter: (p: number) => formatINR(p) },
        })
        const series = chart.addCandlestickSeries({
          upColor: "#22c55e", downColor: "#ef4444", borderVisible: false,
          wickUpColor: "#22c55e", wickDownColor: "#ef4444",
          priceFormat: { type: "custom", formatter: (p: number) => formatINR(p), minMove: 0.01 },
        })
        series.setData(
          json.candles.map((c: any) => ({
            time: c.time, open: c.open_inr, high: c.high_inr, low: c.low_inr, close: c.close_inr,
          }))
        )

        if (json.markers?.length) {
          series.setMarkers(
            json.markers.map((m: any) => ({
              time: m.time,
              position: m.signal_type === "LONG" ? "belowBar" : "aboveBar",
              color: m.signal_type === "LONG" ? "#22c55e" : "#ef4444",
              shape: m.signal_type === "LONG" ? "arrowUp" : "arrowDown",
              text: `${m.signal_type} (${m.quality})`,
            }))
          )
        }

        const handleResize = () => {
          if (containerRef.current) chart.applyOptions({ width: containerRef.current.clientWidth })
        }
        window.addEventListener("resize", handleResize)
        setLoading(false)
        return () => window.removeEventListener("resize", handleResize)
      } catch (e) {
        console.error("Failed to render chart:", e)
        setError("Failed to load chart data.")
        setLoading(false)
      }
    }

    build()
    return () => {
      cancelled = true
      chart?.remove?.()
    }
  }, [timeframe])

  return (
    <div className="min-h-screen">
      <Navigation currentPage="chart" />
      <main className="container mx-auto px-4 py-6">
        <div className="mb-6 flex items-center justify-between flex-wrap gap-2">
          <div>
            <h1 className="text-2xl font-bold">Live Chart</h1>
            <p className="text-muted-foreground text-xs mt-1">
              BTC/USDT PERPETUAL &middot; prices in INR
              {conversionSource && ` via ${conversionSource} (${conversionStatus})`}
            </p>
          </div>
          <div className="flex gap-1">
            {TIMEFRAMES.map((tf) => (
              <button
                key={tf}
                onClick={() => setTimeframe(tf)}
                className={`px-3 py-1.5 rounded text-sm ${
                  timeframe === tf ? "bg-primary text-black font-semibold" : "bg-muted text-muted-foreground"
                }`}
              >
                {tf}
              </button>
            ))}
          </div>
        </div>

        <div className="dashboard-card relative">
          {loading && <div className="absolute inset-4 bg-muted rounded animate-pulse z-10" />}
          {error && !loading && <p className="text-muted-foreground text-sm text-center py-24">{error}</p>}
          <div ref={containerRef} style={{ display: error ? "none" : "block" }} />
        </div>
      </main>
    </div>
  )
}
