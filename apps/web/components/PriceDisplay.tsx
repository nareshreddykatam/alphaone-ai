import { formatINR } from "@/lib/currency"

interface PriceDisplayProps {
  price: number | null
  change?: number
  changePct?: number
}

export function PriceDisplay({ price, change, changePct }: PriceDisplayProps) {
  const isPositive = (change || 0) >= 0

  return (
    <div className="flex items-baseline space-x-2">
      <span className="text-3xl font-bold font-mono">
        {formatINR(price)}
      </span>
      {change !== undefined && (
        <span className={`text-sm font-mono ${isPositive ? "text-long" : "text-short"}`}>
          {isPositive ? "+" : ""}{change.toFixed(2)} ({isPositive ? "+" : ""}{changePct?.toFixed(2)}%)
        </span>
      )}
    </div>
  )
}
