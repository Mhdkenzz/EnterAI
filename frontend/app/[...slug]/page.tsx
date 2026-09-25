// Every workspace URL (/projects/<id>, /inbox, ...) renders the same client app,
// which reads the path itself (lib/routes.ts). Static routes such as /sso/callback
// still take precedence over this catch-all.
export { default } from "../page";
