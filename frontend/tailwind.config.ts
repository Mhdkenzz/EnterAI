import type { Config } from "tailwindcss";
export default { darkMode: ["class"], content: ["./app/**/*.{ts,tsx}","./components/**/*.{ts,tsx}"], theme: { extend: { colors: { canvas: "#09090b", panel: "#101014", muted: "#18181d", line: "#26262d", ink: "#f6f6f7", violet: "#9d7aff" }, boxShadow: { glow: "0 0 38px rgba(139,92,246,.18)" } } }, plugins: [] } satisfies Config;
