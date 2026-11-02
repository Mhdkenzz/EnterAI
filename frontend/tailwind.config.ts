import type { Config } from "tailwindcss";

export default {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: "#f5f7fb",
        panel: "#ffffff",
        muted: "#f0f3f8",
        line: "#e1e7f0",
        ink: "#17243a",
        accent: "#315fce",
        "accent-hover": "#244fae",
        nav: "#12213a",
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
