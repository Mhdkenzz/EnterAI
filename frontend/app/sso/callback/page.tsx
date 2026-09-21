"use client";
import { useEffect } from "react";

/** The API finishes OIDC then redirects here; move the one-time fragment payload
 * to the main auth screen, which consumes it and removes it from the URL. */
export default function SsoCallback(){
  useEffect(()=>{ window.location.replace(`/${window.location.hash}`); },[]);
  return <main className="grid min-h-screen place-items-center bg-canvas p-6 text-sm text-zinc-400">Completing SSO sign-in...</main>;
}
