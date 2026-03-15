"""
Модели для AI Support MVP.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class IntentResult:
    """Результат классификации намерения пользователя."""
    intent: str
    confidence: float
    maybe_reason: Optional[str] = None
