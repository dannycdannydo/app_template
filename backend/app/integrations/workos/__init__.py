"""WorkOS provider adapters (Scope §6.3 onwards, blueprint §5, §30, ADR-0001).

The Management API key and webhook secret live only in the adapters in this
package, never in routers, schemas or frontend variables. ``organizations.py``
ships with this work unit; ``invitations.py`` joins in the invitations work
unit (Scope §6.5).
"""

from __future__ import annotations
