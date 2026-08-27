// Single centralized INR formatter -- AlphaOne is INR-only in the UI
// (the user's CoinDCX futures account is INR-margined). Never scatter
// `$${value}` or ad-hoc toLocaleString() currency formatting in components;
// import formatINR from here instead.
//
// Uses Intl.NumberFormat('en-IN') for correct Indian digit grouping
// (lakhs/crores, e.g. ₹1,00,000.00) rather than reimplementing it.

const inrFormatter = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
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
