import type { Config } from "tailwindcss"

const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        background: "#0a0a0f",
        foreground: "#e4e4e7",
        card: "#111118",
        "card-foreground": "#e4e4e7",
        muted: "#1c1c27",
        "muted-foreground": "#a1a1aa",
        border: "#1e1e2e",
        input: "#1e1e2e",
        primary: "#22c55e",
        "primary-foreground": "#000",
        secondary: "#3b82f6",
        "secondary-foreground": "#fff",
        destructive: "#ef4444",
        "destructive-foreground": "#fff",
        long: "#22c55e",
        short: "#ef4444",
        "no-trade": "#eab308",
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "monospace"],
      },
    },
  },
  plugins: [],
}
export default config
