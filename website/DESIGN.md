---
name: Zenith Technical
colors:
  surface: '#f7f9fb'
  surface-dim: '#d8dadc'
  surface-bright: '#f7f9fb'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#f2f4f6'
  surface-container: '#eceef0'
  surface-container-high: '#e6e8ea'
  surface-container-highest: '#e0e3e5'
  on-surface: '#191c1e'
  on-surface-variant: '#45464d'
  inverse-surface: '#2d3133'
  inverse-on-surface: '#eff1f3'
  outline: '#76777d'
  outline-variant: '#c6c6cd'
  surface-tint: '#565e74'
  primary: '#000000'
  on-primary: '#ffffff'
  primary-container: '#131b2e'
  on-primary-container: '#7c839b'
  inverse-primary: '#bec6e0'
  secondary: '#505f76'
  on-secondary: '#ffffff'
  secondary-container: '#d0e1fb'
  on-secondary-container: '#54647a'
  tertiary: '#000000'
  on-tertiary: '#ffffff'
  tertiary-container: '#271901'
  on-tertiary-container: '#98805d'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#dae2fd'
  primary-fixed-dim: '#bec6e0'
  on-primary-fixed: '#131b2e'
  on-primary-fixed-variant: '#3f465c'
  secondary-fixed: '#d3e4fe'
  secondary-fixed-dim: '#b7c8e1'
  on-secondary-fixed: '#0b1c30'
  on-secondary-fixed-variant: '#38485d'
  tertiary-fixed: '#fcdeb5'
  tertiary-fixed-dim: '#dec29a'
  on-tertiary-fixed: '#271901'
  on-tertiary-fixed-variant: '#574425'
  background: '#f7f9fb'
  on-background: '#191c1e'
  surface-variant: '#e0e3e5'
typography:
  display-lg:
    fontFamily: Geist
    fontSize: 48px
    fontWeight: '600'
    lineHeight: 56px
    letterSpacing: -0.02em
  headline-lg:
    fontFamily: Geist
    fontSize: 32px
    fontWeight: '600'
    lineHeight: 40px
    letterSpacing: -0.01em
  headline-lg-mobile:
    fontFamily: Geist
    fontSize: 24px
    fontWeight: '600'
    lineHeight: 32px
  body-md:
    fontFamily: Geist
    fontSize: 16px
    fontWeight: '400'
    lineHeight: 24px
  body-sm:
    fontFamily: Geist
    fontSize: 14px
    fontWeight: '400'
    lineHeight: 20px
  code-md:
    fontFamily: JetBrains Mono
    fontSize: 14px
    fontWeight: '400'
    lineHeight: 20px
  label-caps:
    fontFamily: Geist
    fontSize: 12px
    fontWeight: '600'
    lineHeight: 16px
    letterSpacing: 0.05em
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  unit: 4px
  gutter: 24px
  margin-mobile: 16px
  margin-desktop: 32px
  container-max: 1440px
---

## Brand & Style

The design system is engineered for high-performance technical environments, catering to developers, data scientists, and engineers. It prioritizes clarity, precision, and cognitive ease.

The aesthetic follows a **Refined Technical Minimalism**. It moves away from the harshness of traditional "engineering" UIs by utilizing generous white space, subtle tonal shifts, and a sophisticated approach to geometry. By introducing soft-radius edges, we balance the rigors of technical data with a more approachable, human-centric interface. The emotional response should be one of quiet confidence, efficiency, and modern professionalism.

## Colors

This design system utilizes an "epurated" or purified palette, focusing on a monochrome foundation with high-utility accents.

- **Primary:** A deep Slate Blue-Black used for core branding, primary actions, and high-contrast text.
- **Secondary:** A muted Steel Blue used for secondary information, icons, and supporting text.
- **Neutral:** A range of ultra-light cool greys and whites to define surfaces without creating visual noise.
- **Functional:** Success (Emerald), Warning (Amber), and Error (Rose) are used sparingly in desaturated tones to maintain the calm atmosphere while providing necessary feedback.

Surfaces should primarily use high-brightness neutrals to keep the interface feeling lightweight and airy.

## Typography

Typography is the backbone of this technical system. We use **Geist** for its exceptional clarity and modern, geometric aesthetic that remains highly legible in dense interfaces. **JetBrains Mono** is utilized for code snippets, data values, and technical labels to provide a distinct visual anchor for functional data.

Maintain a strict hierarchy. Large display type is reserved for empty states or dashboard overviews. For standard operations, rely on `body-sm` and `body-md` to maximize information density without sacrificing readability.

## Layout & Spacing

The system employs a **Fluid Grid** with fixed constraints at the highest level.

- **Grid:** 12-column layout for desktop, 4-column for mobile.
- **Rhythm:** An 8px base unit (2x the 4px base spacing unit) governs all padding and margins to ensure a consistent vertical and horizontal cadence.
- **Density:** Technical views (tables, editors) should use a "Compact" spacing model (base unit of 4px), while marketing or landing pages should use a "Spacious" model (base unit of 12px or 16px).

## Elevation & Depth

This design system uses **Tonal Layers** rather than heavy shadows to signify depth. This reinforces the "epurated" feel.

- **Level 0 (Background):** Solid white or #F8FAFC.
- **Level 1 (Cards/Panels):** Defined by 1px subtle borders (#E2E8F0) rather than shadows.
- **Level 2 (Popovers/Modals):** High-diffusion, ultra-low opacity shadows (Color: Slate 900, Opacity: 4%, Blur: 20px) combined with a solid 1px border.
- **Interaction:** Hover states are indicated by subtle background tint shifts (e.g., White to Slate-50) rather than vertical lifting.

## Shapes

To evolve the design from a "boxy" technical tool to a modern, professional interface, we have adopted a **Rounded** shape language (8px / 0.5rem base).

- **Base Radius (0.5rem):** Used for buttons, input fields, and small cards.
- **Large Radius (1rem):** Used for primary containers, modals, and larger dashboard cards.
- **Full Radius (Pill):** Used exclusively for status indicators (chips) and toggle switches to differentiate them from actionable buttons.

## Components

### Buttons
Primary buttons use the Slate-900 background with white text. Secondary buttons use a subtle 1px border with no fill. All buttons feature the 8px corner radius.

### Input Fields
Inputs are minimal: a 1px border in Slate-200, Geist body-sm text, and 8px rounded corners. The focus state uses a 1px Slate-900 border, avoid heavy "glow" effects.

### Chips & Badges
Small, pill-shaped elements with low-saturation backgrounds (e.g., light green tint with dark green text) to indicate status without overpowering the content.

### Cards
Cards are the primary container. They should use a 1px border (#E2E8F0) and an 8px (rounded-lg) corner radius. Avoid shadows on cards unless they are floating (e.g., draggable elements).

### Lists & Data Tables
Tables should be borderless, using 1px horizontal dividers only. Row hover states should be a subtle Slate-50. Use JetBrains Mono for numeric data to ensure alignment.

### Code Blocks
Containers for code should use a slightly darker background (Slate-50) and the 8px corner radius to distinguish them from standard text blocks.
