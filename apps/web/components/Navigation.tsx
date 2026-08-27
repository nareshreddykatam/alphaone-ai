"use client"

import Link from "next/link"

const navItems = [
  { id: "dashboard", label: "Dashboard", href: "/dashboard" },
  { id: "chart", label: "Live Chart", href: "/chart" },
  { id: "signals", label: "Signals", href: "/signals" },
  { id: "trades", label: "Trades", href: "/trades" },
  { id: "performance", label: "Performance", href: "/performance" },
  { id: "model", label: "Model", href: "/model" },
  { id: "risk", label: "Risk", href: "/risk" },
  { id: "settings", label: "Settings", href: "/settings" },
]

interface NavigationProps {
  currentPage: string
}

export function Navigation({ currentPage }: NavigationProps) {
  return (
    <nav className="border-b border-border bg-card/80 backdrop-blur-sm sticky top-0 z-50">
      <div className="container mx-auto px-4">
        <div className="flex items-center justify-between h-14">
          <div className="flex items-center space-x-2">
            <div className="w-8 h-8 bg-primary rounded-lg flex items-center justify-center">
              <span className="text-black font-bold text-sm">A1</span>
            </div>
            <span className="font-bold text-lg">AlphaOne</span>
            <span className="text-xs text-muted-foreground bg-muted px-2 py-0.5 rounded">
              BTC AI
            </span>
          </div>

          <div className="hidden md:flex items-center space-x-1">
            {navItems.map((item) => (
              <Link
                key={item.id}
                href={item.href}
                className={`px-3 py-2 text-sm rounded-lg transition-colors ${
                  currentPage === item.id
                    ? "bg-muted text-foreground"
                    : "text-muted-foreground hover:text-foreground hover:bg-muted/50"
                }`}
              >
                {item.label}
              </Link>
            ))}
          </div>

          <div className="flex items-center space-x-2">
            <div className="w-2 h-2 bg-green-500 rounded-full animate-pulse" />
            <span className="text-xs text-muted-foreground">Paper</span>
          </div>
        </div>

        <div className="md:hidden flex overflow-x-auto pb-2 -mx-4 px-4 space-x-1">
          {navItems.map((item) => (
            <Link
              key={item.id}
              href={item.href}
              className={`px-3 py-1.5 text-xs rounded-lg whitespace-nowrap transition-colors ${
                currentPage === item.id
                  ? "bg-muted text-foreground"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {item.label}
            </Link>
          ))}
        </div>
      </div>
    </nav>
  )
}
