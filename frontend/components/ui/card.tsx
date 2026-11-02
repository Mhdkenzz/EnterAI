import { ComponentProps } from "react";
import { cn } from "@/lib/utils";

export function Card({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("app-card rounded-card border", className)} {...props} />;
}
