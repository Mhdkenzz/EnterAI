import type { Config } from "tailwindcss";

export default {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: "rgb(var(--canvas-rgb) / <alpha-value>)",
        panel: "var(--surface)",
        muted: "var(--surface-subtle)",
        line: "rgb(var(--line-rgb) / <alpha-value>)",
        ink: "var(--ink)",
        accent: "rgb(var(--accent-rgb) / <alpha-value>)",
        "accent-hover": "var(--accent-hover)",
        nav: "var(--nav)",
      },
      boxShadow: {
        card: "0 8px 24px rgba(21, 38, 67, 0.045), 0 1px 2px rgba(21, 38, 67, 0.035)",
        float: "0 12px 32px rgba(21, 38, 67, 0.16)",
      },
      borderRadius: { card: "14px" },
    },
  },
  plugins: [],
} satisfies Config;
