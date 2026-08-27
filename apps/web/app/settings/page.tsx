"use client"

import { useState, useEffect } from "react"
import { Navigation } from "@/components/Navigation"
import { parseUtcDate } from "@/lib/time"

const API = process.env.NEXT_PUBLIC_API_URL

const CONNECTION_LABELS: Record<string, string> = {
  LIVE: "Connected",
  STALE: "Stale (was connected, data aging)",
  DISCONNECTED: "Disconnected (sync failing)",
  NOT_CONFIGURED: "Not Configured",
  NOT_CONNECTED: "Not Connected",
}

export default function SettingsPage() {
  const [settings, setSettings] = useState<any>(null)
  const [account, setAccount] = useState<any>(null)
  const [syncStatus, setSyncStatus] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [syncing, setSyncing] = useState(false)
  const [depositAmount, setDepositAmount] = useState("")
  const [withdrawalAmount, setWithdrawalAmount] = useState("")
  const [message, setMessage] = useState<string | null>(null)

  const fetchAll = async () => {
    try {
      const [s, a, sync] = await Promise.all([
        fetch(`${API}/api/v1/settings/`).then((r) => r.json()),
        fetch(`${API}/api/v1/accounts/`).then((r) => r.json()),
        fetch(`${API}/api/v1/accounts/sync-status`).then((r) => r.json()),
      ])
      setSettings(s)
      setAccount(a)
      setSyncStatus(sync)
    } catch (e) {
      console.error("Failed to fetch settings:", e)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchAll()
  }, [])

  const triggerSync = async () => {
    setSyncing(true)
    try {
      await fetch(`${API}/api/v1/accounts/sync`, { method: "POST" })
      await fetchAll()
    } finally {
      setSyncing(false)
    }
  }

  const recordDeposit = async (e: React.FormEvent) => {
    e.preventDefault()
    await fetch(`${API}/api/v1/accounts/deposits`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ amount: parseFloat(depositAmount) }),
    })
    setDepositAmount("")
    setMessage("Deposit recorded.")
  }

  const recordWithdrawal = async (e: React.FormEvent) => {
    e.preventDefault()
    await fetch(`${API}/api/v1/accounts/withdrawals`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ amount: parseFloat(withdrawalAmount) }),
    })
    setWithdrawalAmount("")
    setMessage("Withdrawal recorded.")
  }

  if (loading) {
    return (
      <div className="min-h-screen">
        <Navigation currentPage="settings" />
        <main className="container mx-auto px-4 py-6">
          <div className="h-24 bg-muted rounded animate-pulse" />
        </main>
      </div>
    )
  }

  return (
    <div className="min-h-screen">
      <Navigation currentPage="settings" />
      <main className="container mx-auto px-4 py-6 space-y-6 max-w-2xl">
        <div>
          <h1 className="text-2xl font-bold">Settings</h1>
          <p className="text-muted-foreground text-sm">Account, risk configuration, and cash movements.</p>
        </div>

        {message && <div className="px-4 py-2 rounded-lg border border-long text-long text-sm">{message}</div>}

        <section className="dashboard-card space-y-3">
          <div className="flex items-center justify-between">
            <p className="stat-label">CoinDCX Connection</p>
            <button
              onClick={triggerSync}
              disabled={syncing}
              className="px-3 py-1.5 rounded bg-muted text-xs border border-border disabled:opacity-50"
            >
              {syncing ? "Syncing..." : "Sync Now"}
            </button>
          </div>
          <p className="font-mono text-sm">
            Connection:{" "}
            <span className={account?.connection_status === "LIVE" ? "text-long" : "text-no-trade"}>
              {CONNECTION_LABELS[account?.connection_status] || account?.connection_status}
            </span>
          </p>
          <p className="font-mono text-sm text-muted-foreground">
            Last sync:{" "}
            {syncStatus?.last_sync
              ? `${parseUtcDate(syncStatus.last_sync.timestamp).toLocaleString()}${syncStatus?.is_stale ? " (stale)" : ""}`
              : "never"}
          </p>
          {account?.connection_status === "NOT_CONFIGURED" && (
            <p className="text-xs text-muted-foreground">
              Set COINDCX_API_KEY and COINDCX_API_SECRET as server-side environment variables to connect
              (never entered here in the browser -- see docs/deployment.md).
            </p>
          )}
        </section>

        <section className="dashboard-card space-y-2 font-mono text-sm">
          <p className="stat-label">Risk Configuration (read-only, set via server config)</p>
          <p>Trading Mode: {settings?.trading_mode}</p>
          <p>Risk Per Trade: {settings?.risk_per_trade_pct}%</p>
          <p>Max Daily Loss: {settings?.max_daily_loss_pct}%</p>
          <p>Max Leverage: {settings?.max_leverage}x</p>
        </section>

        <section className="dashboard-card space-y-3">
          <p className="stat-label">Record a Deposit</p>
          <form onSubmit={recordDeposit} className="flex gap-2">
            <input required type="number" step="any" placeholder="Amount (INR)"
              className="bg-muted rounded px-3 py-2 text-sm flex-1"
              value={depositAmount} onChange={(e) => setDepositAmount(e.target.value)} />
            <button type="submit" className="px-4 py-2 rounded-lg bg-primary text-black text-sm font-semibold">Add</button>
          </form>
        </section>

        <section className="dashboard-card space-y-3">
          <p className="stat-label">Record a Withdrawal</p>
          <form onSubmit={recordWithdrawal} className="flex gap-2">
            <input required type="number" step="any" placeholder="Amount (INR)"
              className="bg-muted rounded px-3 py-2 text-sm flex-1"
              value={withdrawalAmount} onChange={(e) => setWithdrawalAmount(e.target.value)} />
            <button type="submit" className="px-4 py-2 rounded-lg bg-muted text-sm font-semibold border border-border">Add</button>
          </form>
          <p className="text-xs text-muted-foreground">
            Deposits/withdrawals are tracked separately and never counted as trading P&L.
          </p>
        </section>
      </main>
    </div>
  )
}
