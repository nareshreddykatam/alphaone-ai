"use client"

import React, { useState, useEffect } from "react"
import { Navigation } from "@/components/Navigation"
import { formatINR } from "@/lib/currency"

const API = process.env.NEXT_PUBLIC_API_URL

const MATCH_LABELS: Record<string, string> = {
  MANUAL: "Manual",
  AUTO_MATCHED: "Auto-matched",
  AMBIGUOUS: "Needs confirmation",
  UNMATCHED: "Unmatched",
  CONFIRMED: "Confirmed",
}

export default function TradesPage() {
  const [trades, setTrades] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [showOpenForm, setShowOpenForm] = useState(false)
  const [exitingId, setExitingId] = useState<string | null>(null)
  const [resolvingId, setResolvingId] = useState<string | null>(null)
  const [candidates, setCandidates] = useState<any[]>([])
  const [error, setError] = useState<string | null>(null)

  const [openForm, setOpenForm] = useState({
    symbol: "BTC/USDT", side: "LONG", entry_price: "", quantity: "", stop_loss: "", take_profit_1: "",
  })
  const [exitForm, setExitForm] = useState({ exit_price: "", quantity: "", reason: "" })

  const fetchTrades = async () => {
    try {
      const res = await fetch(`${API}/api/v1/trades/?limit=100`)
      const json = await res.json()
      setTrades(json.trades || [])
    } catch (e) {
      console.error("Failed to fetch trades:", e)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchTrades()
  }, [])

  const submitOpen = async (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    try {
      const res = await fetch(`${API}/api/v1/journal/open`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol: openForm.symbol,
          side: openForm.side,
          entry_price: parseFloat(openForm.entry_price),
          quantity: parseFloat(openForm.quantity),
          stop_loss: openForm.stop_loss ? parseFloat(openForm.stop_loss) : null,
          take_profit_1: openForm.take_profit_1 ? parseFloat(openForm.take_profit_1) : null,
        }),
      })
      if (!res.ok) {
        const body = await res.json()
        throw new Error(body.detail || "Failed to log trade")
      }
      setShowOpenForm(false)
      setOpenForm({ symbol: "BTC/USDT", side: "LONG", entry_price: "", quantity: "", stop_loss: "", take_profit_1: "" })
      await fetchTrades()
    } catch (e: any) {
      setError(e.message)
    }
  }

  const submitExit = async (tradeId: string, e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    try {
      const res = await fetch(`${API}/api/v1/journal/${tradeId}/exit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          exit_price: parseFloat(exitForm.exit_price),
          quantity: parseFloat(exitForm.quantity),
          reason: exitForm.reason || null,
        }),
      })
      if (!res.ok) {
        const body = await res.json()
        throw new Error(body.detail || "Failed to record exit")
      }
      setExitingId(null)
      setExitForm({ exit_price: "", quantity: "", reason: "" })
      await fetchTrades()
    } catch (e: any) {
      setError(e.message)
    }
  }

  const openResolve = async (tradeId: string) => {
    if (resolvingId === tradeId) {
      setResolvingId(null)
      return
    }
    try {
      const res = await fetch(`${API}/api/v1/journal/${tradeId}/match-candidates`)
      const body = await res.json()
      setCandidates(body.candidates || [])
      setResolvingId(tradeId)
    } catch (e) {
      console.error("Failed to fetch match candidates:", e)
    }
  }

  const confirmMatch = async (tradeId: string, signalId: string, confidence: number) => {
    try {
      await fetch(`${API}/api/v1/journal/${tradeId}/confirm-match`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ signal_id: signalId, confidence }),
      })
      setResolvingId(null)
      await fetchTrades()
    } catch (e) {
      console.error("Failed to confirm match:", e)
    }
  }

  const statusColor = (status: string) =>
    status === "CLOSED" ? "text-muted-foreground" : status === "CANCELLED" ? "text-short" : "text-long"

  return (
    <div className="min-h-screen">
      <Navigation currentPage="trades" />
      <main className="container mx-auto px-4 py-6">
        <div className="mb-6 flex items-center justify-between flex-wrap gap-2">
          <div>
            <h1 className="text-2xl font-bold">Trade Journal</h1>
            <p className="text-muted-foreground text-sm">
              Manual entries + CoinDCX-detected positions -- AlphaOne never places orders on CoinDCX.
            </p>
          </div>
          <button
            onClick={() => setShowOpenForm((v) => !v)}
            className="px-4 py-2 rounded-lg bg-primary text-black text-sm font-semibold"
          >
            {showOpenForm ? "Cancel" : "+ Log Trade"}
          </button>
        </div>

        {error && (
          <div className="mb-4 px-4 py-2 rounded-lg border border-short text-short text-sm">{error}</div>
        )}

        {showOpenForm && (
          <form onSubmit={submitOpen} className="dashboard-card mb-6 grid grid-cols-2 md:grid-cols-6 gap-3">
            <select
              className="bg-muted rounded px-2 py-1.5 text-sm"
              value={openForm.side}
              onChange={(e) => setOpenForm({ ...openForm, side: e.target.value })}
            >
              <option value="LONG">LONG</option>
              <option value="SHORT">SHORT</option>
            </select>
            <input required type="number" step="any" placeholder="Entry price (INR)" className="bg-muted rounded px-2 py-1.5 text-sm"
              value={openForm.entry_price} onChange={(e) => setOpenForm({ ...openForm, entry_price: e.target.value })} />
            <input required type="number" step="any" placeholder="Quantity" className="bg-muted rounded px-2 py-1.5 text-sm"
              value={openForm.quantity} onChange={(e) => setOpenForm({ ...openForm, quantity: e.target.value })} />
            <input type="number" step="any" placeholder="Stop loss (INR)" className="bg-muted rounded px-2 py-1.5 text-sm"
              value={openForm.stop_loss} onChange={(e) => setOpenForm({ ...openForm, stop_loss: e.target.value })} />
            <input type="number" step="any" placeholder="Take profit (INR)" className="bg-muted rounded px-2 py-1.5 text-sm"
              value={openForm.take_profit_1} onChange={(e) => setOpenForm({ ...openForm, take_profit_1: e.target.value })} />
            <button type="submit" className="px-3 py-1.5 rounded bg-primary text-black text-sm font-semibold">Save</button>
          </form>
        )}

        <div className="dashboard-card overflow-x-auto">
          {loading ? (
            <div className="h-24 bg-muted rounded animate-pulse" />
          ) : trades.length === 0 ? (
            <p className="text-muted-foreground text-sm py-8 text-center">No trades logged yet.</p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-muted-foreground border-b border-border">
                  <th className="py-2 pr-4">Trade ID</th>
                  <th className="py-2 pr-4">Source</th>
                  <th className="py-2 pr-4">Side</th>
                  <th className="py-2 pr-4">Status</th>
                  <th className="py-2 pr-4">Entry</th>
                  <th className="py-2 pr-4">Exit</th>
                  <th className="py-2 pr-4">Qty</th>
                  <th className="py-2 pr-4">PnL</th>
                  <th className="py-2 pr-4">R</th>
                  <th className="py-2 pr-4">Match</th>
                  <th className="py-2"></th>
                </tr>
              </thead>
              <tbody>
                {trades.map((t) => (
                  <React.Fragment key={t.trade_id}>
                    <tr className="border-b border-border/50">
                      <td className="py-2 pr-4 font-mono text-xs">{t.trade_id}</td>
                      <td className="py-2 pr-4 text-xs">{t.is_manual_entry ? "Manual" : "CoinDCX"}</td>
                      <td className={`py-2 pr-4 font-bold ${t.side === "LONG" ? "text-long" : "text-short"}`}>{t.side}</td>
                      <td className={`py-2 pr-4 font-mono text-xs ${statusColor(t.status)}`}>{t.status}</td>
                      <td className="py-2 pr-4 font-mono">{formatINR(t.entry_price)}</td>
                      <td className="py-2 pr-4 font-mono">{t.exit_price != null ? formatINR(t.exit_price) : "--"}</td>
                      <td className="py-2 pr-4 font-mono">{t.quantity}</td>
                      <td className={`py-2 pr-4 font-mono ${t.pnl > 0 ? "text-long" : t.pnl < 0 ? "text-short" : ""}`}>
                        {t.pnl != null ? formatINR(t.pnl, { showSign: true }) : "--"}
                      </td>
                      <td className="py-2 pr-4 font-mono">{t.r_multiple ? t.r_multiple.toFixed(2) : "--"}</td>
                      <td className="py-2 pr-4 text-xs">
                        {t.match_status === "AMBIGUOUS" ? (
                          <button onClick={() => openResolve(t.trade_id)} className="text-no-trade underline">
                            {MATCH_LABELS[t.match_status]}
                          </button>
                        ) : (
                          <span className={t.match_status === "UNMATCHED" ? "text-muted-foreground" : ""}>
                            {MATCH_LABELS[t.match_status] || t.match_status || "--"}
                          </span>
                        )}
                      </td>
                      <td className="py-2">
                        {(t.status === "OPEN" || t.status === "PARTIALLY_CLOSED") && (
                          <button
                            onClick={() => setExitingId(exitingId === t.trade_id ? null : t.trade_id)}
                            className="px-2 py-1 rounded bg-muted text-xs"
                          >
                            {exitingId === t.trade_id ? "Cancel" : "Exit"}
                          </button>
                        )}
                      </td>
                    </tr>
                    {exitingId === t.trade_id && (
                      <tr className="border-b border-border/50">
                        <td colSpan={11} className="py-3">
                          <form onSubmit={(e) => submitExit(t.trade_id, e)} className="flex flex-wrap gap-2 items-center">
                            <input required type="number" step="any" placeholder="Exit price (INR)" className="bg-muted rounded px-2 py-1.5 text-sm w-32"
                              value={exitForm.exit_price} onChange={(e) => setExitForm({ ...exitForm, exit_price: e.target.value })} />
                            <input required type="number" step="any" placeholder="Quantity" className="bg-muted rounded px-2 py-1.5 text-sm w-28"
                              value={exitForm.quantity} onChange={(e) => setExitForm({ ...exitForm, quantity: e.target.value })} />
                            <input type="text" placeholder="Reason" className="bg-muted rounded px-2 py-1.5 text-sm w-40"
                              value={exitForm.reason} onChange={(e) => setExitForm({ ...exitForm, reason: e.target.value })} />
                            <button type="submit" className="px-3 py-1.5 rounded bg-primary text-black text-sm font-semibold">
                              Record Exit
                            </button>
                          </form>
                        </td>
                      </tr>
                    )}
                    {resolvingId === t.trade_id && (
                      <tr className="border-b border-border/50">
                        <td colSpan={11} className="py-3">
                          <p className="text-xs text-muted-foreground mb-2">POSSIBLE SIGNAL MATCHES</p>
                          {candidates.length === 0 ? (
                            <p className="text-xs text-muted-foreground">No candidates found.</p>
                          ) : (
                            <div className="space-y-2">
                              {candidates.map((c) => (
                                <div key={c.signal_id} className="flex items-center gap-3 text-xs">
                                  <span className="font-mono">{c.signal_id}</span>
                                  <span className="text-muted-foreground">
                                    confidence {(c.confidence * 100).toFixed(0)}% &middot; {c.time_diff_minutes.toFixed(0)}m away &middot; {c.price_diff_pct.toFixed(2)}% price diff
                                  </span>
                                  <button
                                    onClick={() => confirmMatch(t.trade_id, c.signal_id, c.confidence)}
                                    className="px-2 py-1 rounded bg-primary text-black font-semibold"
                                  >
                                    Use this
                                  </button>
                                </div>
                              ))}
                            </div>
                          )}
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </main>
    </div>
  )
}
