import "./globals.css";
import type { Metadata } from "next";
import Script from "next/script";
export const metadata: Metadata = { title: "Enter AI", description: "AI-native enterprise project management" };
const themeBootstrap = `try { if (localStorage.getItem("enterai-theme") === "dark") document.documentElement.dataset.theme = "dark"; } catch {}`;
export default function RootLayout({children}:{children:React.ReactNode}) { return <html lang="en" suppressHydrationWarning><head><Script id="theme-bootstrap" strategy="beforeInteractive" dangerouslySetInnerHTML={{ __html: themeBootstrap }} /></head><body>{children}</body></html>; }
