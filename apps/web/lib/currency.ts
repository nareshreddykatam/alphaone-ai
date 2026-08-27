// Centralized currency formatters.
//
// formatINR: for genuinely INR-native amounts (the user's CoinDCX futures
// account equity/margin/P&L, which is INR-margined) and as the SECONDARY
// converted representation of a USDT trading price. Never scatter `$${value}`
// or ad-hoc toLocaleString() currency formatting in components; import from
// here instead. Uses Intl.NumberFormat('en-IN') for correct Indian digit
// grouping (lakhs/crores, e.g. ₹1,00,000.00) rather than reimplementing it.
//
// formatUSDT: the actual CoinDCX BTC/USDT Perpetual trading instrument is
// quoted in USDT -- USDT is the PRIMARY/authoritative denomination for the
// live price and every signal level (Entry/SL/TP). Never replace a USDT
// trading price with an INR-only value; INR is always secondary/additional.

const inrFormatter = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

const usdtFormatter = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

export interface FormatINROptions {
  // Prefix a "+" for positive values (for P&L display). Zero never gets a sign.
  showSign?: boolean
}

export function formatINR(value: number | null | undefined, options: FormatINROptions = {}): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "N/A"
  const abs = Math.abs(value)
  const formatted = inrFormatter.format(abs)
  if (value < 0) return `-${formatted}`
  if (options.showSign && value > 0) return `+${formatted}`
  return formatted
}

export function formatUSDT(value: number | null | undefined, options: FormatINROptions = {}): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "N/A"
  const abs = Math.abs(value)
  const formatted = `${usdtFormatter.format(abs)} USDT`
  if (value < 0) return `-${formatted}`
  if (options.showSign && value > 0) return `+${formatted}`
  return formatted
}

// Label for the small "verified conversion source" disclosure required
// wherever a USDT-sourced price is shown converted to INR (BTC price,
// signal levels, chart prices). Never invents a value when unavailable --
// callers must have already checked conversionStatus before calling
// formatINR on a possibly-null converted amount.
export function conversionStatusLabel(status: string | null | undefined): string {
  if (!status || status === "UNAVAILABLE") return "INR conversion unavailable"
  if (status === "STALE") return "Conversion rate stale"
  return "Live conversion rate"
}
