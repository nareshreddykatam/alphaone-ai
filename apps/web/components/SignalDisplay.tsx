import { formatINR, formatUSDT } from "@/lib/currency"

interface SignalDisplayProps {
  signal: string | null
  regime: string | null
  entryUsdt?: number | null
  slUsdt?: number | null
  tp1Usdt?: number | null
  entryInr?: number | null
  slInr?: number | null
  tp1Inr?: number | null
  rr?: number | null
  quality?: string | null
}

// USDT is the actual BTC/USDT Perpetual trading level (primary); INR is
// the secondary converted representation, shown only when available --
// never invented in its place.
function Level({ usdt, inr, className }: { usdt?: number | null; inr?: number | null; className?: string }) {
  return (
    <>
      <p className={`font-mono font-bold ${className || ""}`}>{formatUSDT(usdt)}</p>
      <p className="text-xs font-mono text-muted-foreground">{inr != null ? `≈ ${formatINR(inr)}` : ""}</p>
    </>
  )
}

const QUALITY_STYLES: Record<string, string> = {
  HIGH: "text-long",
  MEDIUM: "text-no-trade",
  LOW: "text-muted-foreground",
}

export function SignalDisplay({
  signal, regime, entryUsdt, slUsdt, tp1Usdt, entryInr, slInr, tp1Inr, rr, quality,
}: SignalDisplayProps) {
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
            <Level usdt={entryUsdt} inr={entryInr} />
          </div>
          <div>
            <p className="stat-label">Stop Loss</p>
            <Level usdt={slUsdt} inr={slInr} className="text-short" />
          </div>
          <div>
            <p className="stat-label">Take Profit</p>
            <Level usdt={tp1Usdt} inr={tp1Inr} className="text-long" />
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
