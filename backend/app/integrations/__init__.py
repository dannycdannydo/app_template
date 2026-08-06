"""Provider adapters (blueprint §5, §30, ADR-0001).

Every third-party integration lives behind an adapter in this package so the
rest of the application never imports a provider SDK directly and provider
credentials stay server-side. See ``organizations.py`` for the WorkOS
organisation mapping adapter; the invitations adapter joins in the invitations
work unit (Scope §6.5).
"""

from __future__ import annotations
