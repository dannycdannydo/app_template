# ADR 0005: Use shadcn-vue for the Design System

Status: Accepted

## Context

The template needs a consistent, accessible UI component layer with design tokens, dark-mode support, and the ability to customise components per application without fighting a framework abstraction.

## Options considered

- **shadcn-vue**: copy-in components built on Radix Vue primitives and Tailwind CSS; full source ownership, easy theme customisation, no runtime component-library dependency.
- **Naive UI / Element Plus**: full component libraries with rich defaults, but their design systems and styling are harder to override, and components are framework-managed rather than owned.
- **Headless UI + hand-rolled components**: maximum control, but every application re-derives the same components, which the template is meant to prevent.
- **PrimeVue**: powerful but a large surface and a distinct styling paradigm.

## Decision

Use **shadcn-vue** (Tailwind-based, Radix primitives) as the component layer, with design tokens defined in Tailwind configuration. Base components (`button`, `card`, `input`, `label`) are vendored into the template; applications extend rather than replace them.

## Consequences

- Components live in the application source, so teams can customise styling and behaviour without library limitations.
- The template owns accessibility and keyboard behaviour via Radix Vue primitives.
- Teams must keep vendored components consistent and avoid forking them into divergent versions across applications.

---
