"""Demonstration feature module for the AI platform (v0.7 Scope §6.6).

A thin example consumer of the provider-neutral AI layer: it owns its own
routes, schemas and permission gate and calls ``AIService.execute`` through the
demonstration service. It is the template's worked example of how a derived
feature module integrates classification, not a generic AI administration or
arbitrary-prompt surface (v0.7 Scope §6.6).
"""
