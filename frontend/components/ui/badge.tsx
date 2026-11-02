import { cn } from "@/lib/utils";

export function Badge({
  children,
  tone = "default",
}: {
  children: React.ReactNode;
  tone?: "default" | "green" | "amber" | "red" | "blue";
}) {
  const map = {
    default: "border-slate-200 bg-slate-100 text-slate-700",
    green: "border-emerald-200 bg-emerald-50 text-emerald-800",
    amber: "border-amber-200 bg-amber-50 text-amber-800",
    red: "border-rose-200 bg-rose-50 text-rose-800",
    blue: "border-blue-200 bg-blue-50 text-blue-800",
  };
  return (
    <span className={cn("inline-flex items-center rounded-full border px-2.5 py-1 text-[11px] font-semibold leading-none capitalize", map[tone])}>
      {children}
    </span>
  );
}
