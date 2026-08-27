import { formatINR } from "@/lib/currency"

interface SignalDisplayProps {
  signal: string | null
  regime: string | null
  entryInr?: number | null
  slInr?: number | null
  tp1Inr?: number | null
  rr?: number | null
  quality?: string | null
}

const QUALITY_STYLES: Record<string, string> = {
  HIGH: "text-long",
  MEDIUM: "text-no-trade",
  LOW: "text-muted-foreground",
}

export function SignalDisplay({ signal, regime, entryInr, slInr, tp1Inr, rr, quality }: SignalDisplayProps) {
  const signalColor =
    signal === "LONG"
      ? "bg-long"
      : signal === "SHORT"
      ? "bg-short"
      : "bg-no-trade"

  return (
    <div className="dashboard-card">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold">Current Signal</h2>
        <div className={`px-3 py-1 rounded-full text-black font-bold text-sm ${signalColor}`}>
          {signal || "NO TRADE"}
        </div>
      </div>

      {signal && signal !== "NO_TRADE" && (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
          <div>
            <p className="stat-label">Entry</p>
            <p className="font-mono font-bold">{formatINR(entryInr)}</p>
          </div>
          <div>
            <p className="stat-label">Stop Loss</p>
            <p className="font-mono font-bold text-short">{formatINR(slInr)}</p>
          </div>
          <div>
            <p className="stat-label">Take Profit</p>
            <p className="font-mono font-bold text-long">{formatINR(tp1Inr)}</p>
          </div>
          <div>
            <p className="stat-label">Risk/Reward</p>
            <p className="font-mono font-bold">1:{rr || "--"}</p>
          </div>
          <div>
            <p className="stat-label">Quality</p>
            <p className={`font-mono font-bold ${quality ? QUALITY_STYLES[quality] : ""}`}>
              {quality || "--"}
            </p>
          </div>
        </div>
      )}

      {regime && (
        <div className="mt-4">
          <p className="stat-label">Market Regime</p>
          <p className="font-mono">{regime}</p>
        </div>
      )}
    </div>
  )
}
