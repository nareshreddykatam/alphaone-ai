"use client"

import { useState, useEffect, useRef } from "react"
import { Navigation } from "@/components/Navigation"
import { formatINR, formatUSDT } from "@/lib/currency"

const API = process.env.NEXT_PUBLIC_API_URL
const TIMEFRAMES = ["15m", "1h", "4h", "1d"]

// Distinct color for the currently-forming (not-yet-closed) candle, so it
// never looks like an ordinary completed green/red bar -- see Phase 6:
// "The chart should distinguish COMPLETED CANDLES from CURRENT FORMING
// CANDLE." lightweight-charts v4 supports a per-point color override on
// CandlestickData, used only for this one bar.
const FORMING_CANDLE_COLOR = "#eab308"

export default function ChartPage() {
  const containerRef = useRef<HTMLDivElement>(null)
  const [timeframe, setTimeframe] = useState("4h")
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [conversionStatus, setConversionStatus] = useState<string | null>(null)
  const [conversionSource, setConversionSource] = useState<string | null>(null)
  const [formingCandle, setFormingCandle] = useState<any>(null)
  const [marketDataStatus, setMarketDataStatus] = useState<string | null>(null)

  useEffect(() => {
    let chart: any
    let cancelled = false
    let pollTimer: ReturnType<typeof setInterval> | undefined

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
        setMarketDataStatus(json.market_data_status ?? null)
        setFormingCandle(json.forming_candle ?? null)

        if (!json.candles || json.candles.length === 0) {
          setError("No candle data ingested yet for this symbol/timeframe.")
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
          // USDT is the actual CoinDCX BTC/USDT Perpetual trading price --
          // the primary axis. INR is shown separately (legend + forming-
          // candle badge below), never substituted onto the price axis.
          localization: { priceFormatter: (p: number) => formatUSDT(p) },
        })
        const series = chart.addCandlestickSeries({
          upColor: "#22c55e", downColor: "#ef4444", borderVisible: false,
          wickUpColor: "#22c55e", wickDownColor: "#ef4444",
          priceFormat: { type: "custom", formatter: (p: number) => formatUSDT(p), minMove: 0.01 },
        })

        const historicalBars = json.candles.map((c: any) => ({
          time: c.time, open: c.open, high: c.high, low: c.low, close: c.close,
        }))
        // The forming candle is the chart's newest bar, given a distinct
        // color so it never reads as an ordinary completed bar. lightweight-
        // charts requires strictly ascending, unique bar times -- if the
        // candle-ingestion job has already stored a row for this same
        // still-open bucket (ccxt/Binance can return the currently-forming
        // candle as the last page of fetch_ohlcv; see
        // docs/known_limitations.md), the forming candle REPLACES that row
        // rather than being appended as a second point at the same time,
        // since the live tick is the fresher of the two for a bucket that
        // hasn't actually closed yet.
        let bars = historicalBars
        if (json.forming_candle) {
          const formingBar = {
            time: json.forming_candle.time,
            open: json.forming_candle.open, high: json.forming_candle.high,
            low: json.forming_candle.low, close: json.forming_candle.close,
            color: FORMING_CANDLE_COLOR, borderColor: FORMING_CANDLE_COLOR, wickColor: FORMING_CANDLE_COLOR,
          }
          const lastHistorical = historicalBars[historicalBars.length - 1]
          bars = lastHistorical && lastHistorical.time === formingBar.time
            ? [...historicalBars.slice(0, -1), formingBar]
            : [...historicalBars, formingBar]
        }
        series.setData(bars)

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

        // Poll for a fresh forming candle without re-downloading/re-rendering
        // the whole historical series -- only the timeframe the shared
        // aggregator tracks (4h) will ever come back with a non-null
        // forming_candle (see apps/api/routers/market.py).
        if (timeframe === "4h") {
          pollTimer = setInterval(async () => {
            try {
              const pollRes = await fetch(`${API}/api/v1/market/candles?symbol=BTC/USDT&timeframe=4h&limit=1`)
              const pollJson = await pollRes.json()
              if (cancelled) return
              setMarketDataStatus(pollJson.market_data_status ?? null)
              setFormingCandle(pollJson.forming_candle ?? null)
              if (pollJson.forming_candle) {
                series.update({
                  time: pollJson.forming_candle.time,
                  open: pollJson.forming_candle.open, high: pollJson.forming_candle.high,
                  low: pollJson.forming_candle.low, close: pollJson.forming_candle.close,
                  color: FORMING_CANDLE_COLOR, borderColor: FORMING_CANDLE_COLOR, wickColor: FORMING_CANDLE_COLOR,
                })
              }
            } catch (e) {
              console.error("Failed to poll live forming candle:", e)
            }
          }, 10000)
        }

        return () => {
          window.removeEventListener("resize", handleResize)
          if (pollTimer) clearInterval(pollTimer)
        }
      } catch (e) {
        console.error("Failed to render chart:", e)
        setError("Failed to load chart data.")
        setLoading(false)
      }
    }

    build()
    return () => {
      cancelled = true
      if (pollTimer) clearInterval(pollTimer)
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
              CoinDCX BTC/USDT PERPETUAL &middot; prices in USDT (primary)
              {conversionSource && `, INR via ${conversionSource} (${conversionStatus})`}
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

        {timeframe === "4h" && !loading && !error && (
          formingCandle ? (
            <div className="dashboard-card mt-4 border border-[#eab308]/50">
              <div className="flex items-center gap-2 mb-2">
                <span className="w-2 h-2 rounded-full bg-[#eab308] animate-pulse" />
                <p className="text-xs font-mono font-semibold text-[#eab308]">LIVE FORMING 4H CANDLE (not yet closed)</p>
              </div>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
                <div>
                  <p className="stat-label">Open</p>
                  <p className="font-mono">{formatUSDT(formingCandle.open)}</p>
                  <p className="font-mono text-xs text-muted-foreground">{formingCandle.open_inr != null ? `≈ ${formatINR(formingCandle.open_inr)}` : ""}</p>
                </div>
                <div>
                  <p className="stat-label">High</p>
                  <p className="font-mono">{formatUSDT(formingCandle.high)}</p>
                  <p className="font-mono text-xs text-muted-foreground">{formingCandle.high_inr != null ? `≈ ${formatINR(formingCandle.high_inr)}` : ""}</p>
                </div>
                <div>
                  <p className="stat-label">Low</p>
                  <p className="font-mono">{formatUSDT(formingCandle.low)}</p>
                  <p className="font-mono text-xs text-muted-foreground">{formingCandle.low_inr != null ? `≈ ${formatINR(formingCandle.low_inr)}` : ""}</p>
                </div>
                <div>
                  <p className="stat-label">Current</p>
                  <p className="font-mono">{formatUSDT(formingCandle.close)}</p>
                  <p className="font-mono text-xs text-muted-foreground">{formingCandle.close_inr != null ? `≈ ${formatINR(formingCandle.close_inr)}` : ""}</p>
                </div>
              </div>
              <p className="text-xs text-muted-foreground mt-2">
                {formingCandle.tick_count} live tick{formingCandle.tick_count === 1 ? "" : "s"} received this bar &middot; intrabar, not yet a confirmed closed-candle signal condition
              </p>
            </div>
          ) : (
            <p className="text-muted-foreground text-xs mt-3">
              {marketDataStatus === "LIVE"
                ? "Waiting for the first live tick of this 4h bar..."
                : "No forming-candle data -- live market data is not currently LIVE (see Dashboard for connection status)."}
            </p>
          )
        )}
      </main>
    </div>
  )
}
