"""Org-scoped notifications module (Scope §6.3, blueprint §20).

In-app notifications plus their durable email deliveries. Every notification
row hangs off exactly one organisation and one recipient user, so every query
filters on both first (the isolation boundary: another organisation's or
another user's notification simply is not found, 404). Email is only ever sent
from the worker task in this module (``tasks.py``), never from an HTTP handler
(blueprint §20, proven structurally by the email test suite).
"""
