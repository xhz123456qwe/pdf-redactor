"""Offline, local PDF redaction application."""

from .models import ProcessingMode, RedactionColor, RedactionRequest, RedactionResult
from .processor import PDFRedactor

__version__ = "1.1.0"

__all__ = [
    "PDFRedactor",
    "ProcessingMode",
    "RedactionColor",
    "RedactionRequest",
    "RedactionResult",
    "__version__",
]
