import type { Config } from "tailwindcss";
export default { darkMode: ["class"], content: ["./app/**/*.{ts,tsx}","./components/**/*.{ts,tsx}"], theme: { extend: { colors: { canvas: "#09090b", panel: "#101014", muted: "#18181d", line: "#29292f", ink: "#f6f6f7", accent: "#38bdf8" }, boxShadow: { glow: "0 0 28px rgba(56,189,248,.12)" } } }, plugins: [] } satisfies Config;
