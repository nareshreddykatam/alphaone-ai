import type { Metadata } from "next"
import "./globals.css"

export const metadata: Metadata = {
  title: "AlphaOne BTC AI",
  description: "AI-powered BTC/USDT perpetual futures trading intelligence",
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="en" className="dark">
      <body className="bg-background text-foreground min-h-screen antialiased">
        {children}
      </body>
    </html>
  )
}
