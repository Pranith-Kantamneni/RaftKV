/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        console: {
          950: "#080b10",
          900: "#0c1118",
          850: "#101720",
          800: "#131c26",
          700: "#1b2733",
          600: "#263543",
          500: "#3a4c5c",
          400: "#5c7285",
          300: "#8ba0b2",
          200: "#b8c7d3",
          100: "#e2eaf1",
        },
        accent: {
          500: "#22d3ee",
          400: "#4de3f5",
          300: "#8bf0fb",
        },
        signal: {
          green: "#34d399",
          amber: "#fbbf24",
          red: "#fb7185",
          violet: "#a78bfa",
          blue: "#60a5fa",
        },
      },
      fontFamily: {
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "sans-serif",
        ],
        mono: [
          "JetBrains Mono",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Consolas",
          "monospace",
        ],
      },
      boxShadow: {
        panel: "0 1px 0 rgba(255,255,255,0.03) inset, 0 12px 32px rgba(0,0,0,0.25)",
        glow: "0 0 0 1px rgba(34,211,238,0.25), 0 0 24px rgba(34,211,238,0.08)",
      },
      keyframes: {
        "pulse-dot": {
          "0%, 100%": { opacity: "1", transform: "scale(1)" },
          "50%": { opacity: "0.45", transform: "scale(0.85)" },
        },
        "fade-in": {
          from: { opacity: "0", transform: "translateY(4px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        "pulse-dot": "pulse-dot 1.8s ease-in-out infinite",
        "fade-in": "fade-in 180ms ease-out",
      },
    },
  },
  plugins: [],
};
