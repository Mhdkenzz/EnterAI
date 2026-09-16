import "./globals.css";
import type { Metadata } from "next";
export const metadata: Metadata = { title: "Enter AI", description: "AI-native enterprise project management" };
export default function RootLayout({children}:{children:React.ReactNode}) { return <html lang="en" className="dark"><body>{children}</body></html>; }
