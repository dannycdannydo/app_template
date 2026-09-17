"""Untrusted-upload scanning boundary (plan P6, blueprint §30 file security).

``ContentScanner`` is the only seam between the file-processing worker and a
malware/content scanner. The template ships the reviewed ``none`` position
(``NoScanner`` resolves every upload to ``NOT_REQUIRED``); a real scanner is a
separate, provider-selected review that adds one adapter here without touching
the file lifecycle. The worker gates ``ready`` — and therefore AI reads and
downloads — on the verdict, and a scanner outage leaves content quarantined
rather than trusted.
"""

from app.scanning.base import (
    ContentScanner,
    NoScanner,
    ScannerUnavailableError,
    ScanVerdict,
)
from app.scanning.factory import get_scanner

__all__ = [
    "ContentScanner",
    "NoScanner",
    "ScanVerdict",
    "ScannerUnavailableError",
    "get_scanner",
]
