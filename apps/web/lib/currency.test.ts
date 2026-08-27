import { describe, it, expect } from "vitest"
import { formatINR, formatUSDT, conversionStatusLabel } from "./currency"

describe("formatINR", () => {
  it("formats a basic amount with Indian grouping", () => {
    expect(formatINR(1600)).toBe("₹1,600.00")
  })

  it("formats a lakh with Indian grouping", () => {
    expect(formatINR(100000)).toBe("₹1,00,000.00")
  })

  it("formats ten lakh with Indian grouping", () => {
    expect(formatINR(1000000)).toBe("₹10,00,000.00")
  })

  it("formats a crore with Indian grouping", () => {
    expect(formatINR(12345678.9)).toBe("₹1,23,45,678.90")
  })

  it("returns N/A for null", () => {
    expect(formatINR(null)).toBe("N/A")
  })

  it("returns N/A for undefined", () => {
    expect(formatINR(undefined)).toBe("N/A")
  })

  it("returns N/A for NaN", () => {
    expect(formatINR(NaN)).toBe("N/A")
  })

  it("prefixes a minus sign for negative values", () => {
    expect(formatINR(-125)).toBe("-₹125.00")
  })

  it("prefixes a plus sign for positive values when showSign is set", () => {
    expect(formatINR(250.5, { showSign: true })).toBe("+₹250.50")
  })

  it("does not double the sign for negative values when showSign is set", () => {
    expect(formatINR(-125, { showSign: true })).toBe("-₹125.00")
  })

  it("never shows a sign for zero even when showSign is set", () => {
    expect(formatINR(0, { showSign: true })).toBe("₹0.00")
  })

  it("never renders a dollar sign or USD/USDT unit", () => {
    const out = formatINR(78908.9)
    expect(out).not.toMatch(/\$/)
    expect(out).not.toMatch(/USD/)
  })
})

describe("formatUSDT", () => {
  it("formats a basic amount with western grouping and a USDT suffix", () => {
    expect(formatUSDT(79700)).toBe("79,700.00 USDT")
  })

  it("formats large amounts with comma grouping", () => {
    expect(formatUSDT(1234567.8)).toBe("1,234,567.80 USDT")
  })

  it("returns N/A for null", () => {
    expect(formatUSDT(null)).toBe("N/A")
  })

  it("returns N/A for undefined", () => {
    expect(formatUSDT(undefined)).toBe("N/A")
  })

  it("returns N/A for NaN", () => {
    expect(formatUSDT(NaN)).toBe("N/A")
  })

  it("prefixes a minus sign for negative values", () => {
    expect(formatUSDT(-125)).toBe("-125.00 USDT")
  })

  it("prefixes a plus sign for positive values when showSign is set", () => {
    expect(formatUSDT(250.5, { showSign: true })).toBe("+250.50 USDT")
  })

  it("never renders a rupee sign", () => {
    expect(formatUSDT(78908.9)).not.toMatch(/₹/)
  })
})

describe("conversionStatusLabel", () => {
  it("reports unavailable when status is missing", () => {
    expect(conversionStatusLabel(null)).toBe("INR conversion unavailable")
    expect(conversionStatusLabel(undefined)).toBe("INR conversion unavailable")
  })

  it("reports unavailable explicitly", () => {
    expect(conversionStatusLabel("UNAVAILABLE")).toBe("INR conversion unavailable")
  })

  it("reports stale", () => {
    expect(conversionStatusLabel("STALE")).toBe("Conversion rate stale")
  })

  it("reports live", () => {
    expect(conversionStatusLabel("LIVE")).toBe("Live conversion rate")
  })
})
