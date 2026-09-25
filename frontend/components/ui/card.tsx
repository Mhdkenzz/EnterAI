import { ComponentProps } from "react"; import { cn } from "@/lib/utils";
export function Card({className,...props}:ComponentProps<"div">){return <div className={cn("rounded-xl border border-line bg-panel",className)} {...props}/>}
