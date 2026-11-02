---
name: Enter AI Workspace
colors:
  canvas: "#f5f7fb"
  surface: "#ffffff"
  surface-subtle: "#f0f3f8"
  line: "#e1e7f0"
  ink: "#17243a"
  muted: "#627189"
  accent: "#315fce"
  accent-hover: "#244fae"
  nav: "#12213a"
  success: "#14724f"
  warning: "#8b5700"
  danger: "#a93647"
typography:
  font-family: "Inter, ui-sans-serif, system-ui, sans-serif"
  body: "14–15px / 1.55"
  title: "clamp(1.55rem, 2.2vw, 1.9rem) / 1.15, weight 700"
  kicker: "12px / 1, weight 700, uppercase, 0.08em tracking"
spacing:
  base: "4px"
  scale: [4, 8, 12, 16, 20, 24, 32, 40, 48, 64]
  page-gutter: "16px small screens, 24px larger screens"
  card-padding: "16–20px"
radius:
  control: "8px"
  card: "14px"
  pill: "9999px"
elevation:
  card: "0 8px 24px rgba(21, 38, 67, 0.045), 0 1px 2px rgba(21, 38, 67, 0.035)"
  floating: "0 12px 32px rgba(21, 38, 67, 0.16)"
---

# Enter AI design system

## Overview

Enter AI is a calm, information-dense workspace for people coordinating projects and AI agents. The interface should make ownership, status, and the next useful action easy to scan. Use a plain, light canvas for work surfaces and a deep navy sidebar to anchor navigation. Use Enter AI's own blue identity throughout; do not borrow another product's logo, exact palette, or illustrations.

## Colors

Use semantic tokens from `frontend/app/globals.css` and `frontend/tailwind.config.ts`, rather than introducing one-off colors. `canvas` frames the workspace, `surface` holds content, `surface-subtle` groups related regions, and `line` defines quiet boundaries. `ink` is the primary text color; `muted` is supporting text. Use `accent` for primary actions, links, focus indicators, and selected navigation. Pair status colors with text or an icon, never color alone.

Keep contrast readable in both the workspace and overlays. A tinted status panel needs a deliberate dark foreground; translucent pale text on pale surfaces is not acceptable. Avoid gradients and decorative background patterns behind data.

## Typography

Use the system sans stack, with Inter preferred when available. Keep page titles compact and strong, section headings smaller and semibold, and supporting copy at 14–15px with comfortable line height. Uppercase kickers are short labels, not substitutes for headings. Use tabular or bold numerals for dashboard metrics and preserve natural wrapping for longer task and project names.

## Layout and spacing

Use the 4px spacing scale. Align page headers, cards, tables, and forms to the same content gutter. Keep dashboards and forms responsive: collapse multi-column layouts at narrow widths, let data tables scroll horizontally when columns cannot remain readable, and let kanban boards scroll without shrinking cards below a usable width. Use a fixed navigation rail on desktop and a dismissible navigation drawer on small screens. Reserve safe space for persistent floating actions.

Group related information with spacing first and borders second. Do not add grid, dot, or other repeating patterns to the workspace background.

## Elevation and shapes

Use the card shadow for important containers and a stronger floating shadow for dialogs and inspectors. Keep shadows soft and rare. Use 8px control corners, 14px card corners, and pill shapes only for compact badges or status labels. Borders should be cool, light, and low contrast unless indicating focus, selection, or an alert.

## Components

- **Navigation:** dark navy rail, concise labels, clear active state, visible keyboard focus, and an unread marker for Inbox.
- **Buttons:** primary blue for the main action, outlined for secondary actions, ghost for low-emphasis actions, and red only for destructive actions. Preserve focus, disabled, and loading states.
- **Cards:** use shared `Card`; keep headings, dividers, and padding consistent. Hover elevation is subtle and limited to interactive cards.
- **Metrics:** pair a clear value with a short label; icons use restrained semantic tints.
- **Tables and project lists:** stable column alignment, readable row height, clear empty states, and horizontal scrolling on small screens.
- **Tasks and boards:** status and priority are explicit labels; drag and keyboard-accessible move controls remain available. Keep task identity readable when completed.
- **Inbox:** unread state uses both a dot and stronger text weight. Empty inboxes should reassure without adding decoration.
- **Agent chat:** distinguish user and agent messages by alignment and surface tone. Keep the CEO chat shortcut available in the signed-in workspace and open it through the same persistent agent conversation flow.
- **Dialogs and inspectors:** use white surfaces, clear titles, visible close controls, and focused content. Preserve the existing confirmation step for proposed changes.
- **Forms:** labels remain visible, controls share consistent height and focus treatment, and errors appear next to the relevant action in readable text.

## Interaction and accessibility

Use semantic buttons and headings, descriptive accessible names for icon-only controls, and visible `:focus-visible` treatment. Provide status in text as well as color, preserve reduced-motion preferences, and keep interactive targets comfortably large. Empty, loading, error, disabled, and success states are part of each component's design.

## Do and avoid

- Do keep the primary action obvious and the visual hierarchy consistent across Home, Portfolio, My work, Inbox, Teams, Agents, and project views.
- Do use whitespace and light dividers to separate dense data.
- Do keep layouts useful on small screens and preserve content scrolling.
- Avoid dark-mode text colors on white surfaces, low-contrast tinted banners, unnecessary gradients, background grids, and changing semantic colors from screen to screen.
- Avoid reproducing the marks, illustrations, or exact branded treatments of Primer, Shopify, or other reference systems.

## Reference principles

This system applies the general foundations and accessibility guidance in [Primer Design](https://github.com/primer/design) and [Primer Primitives](https://github.com/primer/primitives), the data-heavy product patterns in [Shopify Polaris](https://github.com/Shopify/polaris), the durable documentation structure suggested by [Google Labs' DESIGN.md](https://github.com/google-labs-code/design.md), and the principle of expressing a distinct product identity from [Primer Brand](https://github.com/primer/brand). Enter AI's tokens and components remain original.
