import { ButtonHTMLAttributes, forwardRef } from "react";
import { cn } from "@/lib/utils";
type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "ghost" | "outline" | "danger";
  size?: "sm" | "md";
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant = "primary", size = "md", ...props }, ref) => (
    <button
      ref={ref}
      className={cn(
        "inline-flex shrink-0 items-center justify-center gap-2 rounded-lg font-semibold transition duration-150 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas disabled:pointer-events-none disabled:opacity-50 active:translate-y-px",
        size === "sm" ? "min-h-9 px-3 text-xs" : "min-h-10 px-4 text-sm",
        {
          "bg-accent text-white shadow-sm hover:bg-accent-hover": variant === "primary",
          "bg-transparent text-zinc-500 hover:bg-muted hover:text-ink": variant === "ghost",
          "border border-line bg-panel text-ink shadow-sm hover:border-slate-300 hover:bg-slate-50":
            variant === "outline",
          "border border-rose-200 bg-rose-50 text-rose-700 hover:border-rose-300 hover:bg-rose-100":
            variant === "danger",
        },
        className,
      )}
      {...props}
    />
  ),
);
Button.displayName="Button";
