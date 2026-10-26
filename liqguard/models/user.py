"""User model for storing user data and bindings."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .alert import UserThreshold


@dataclass
class ExchangeBinding:
    """Binding to an exchange (CEX API key or DEX wallet address)."""

    venue: str
    binding_type: str  # "api_key" or "wallet"

    api_key: Optional[str] = None
    api_secret_encrypted: Optional[str] = None

    wallet_address: Optional[str] = None

    created_at: datetime = field(default_factory=datetime.utcnow)
    is_active: bool = True
    last_used: Optional[datetime] = None

    permissions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary (without secrets)."""
        return {
            "venue": self.venue,
            "binding_type": self.binding_type,
            "api_key": self.api_key[:8] + "..." if self.api_key else None,
            "wallet_address": self.wallet_address,
            "created_at": self.created_at.isoformat(),
            "is_active": self.is_active,
            "last_used": self.last_used.isoformat() if self.last_used else None,
            "permissions": self.permissions,
        }


@dataclass
class User:
    """User model with bindings and preferences."""

    telegram_id: int
    username: Optional[str] = None
    language: str = "en"

    bindings: list[ExchangeBinding] = field(default_factory=list)
    threshold: UserThreshold = field(default_factory=UserThreshold)

    is_demo_mode: bool = False

    created_at: datetime = field(default_factory=datetime.utcnow)
    last_active: datetime = field(default_factory=datetime.utcnow)

    def get_binding(self, venue: str) -> Optional[ExchangeBinding]:
        """Get active binding for a venue."""
        for binding in self.bindings:
            if binding.venue == venue and binding.is_active:
                return binding
        return None

    def add_binding(self, binding: ExchangeBinding) -> None:
        """Add or replace a binding for a venue."""
        self.bindings = [b for b in self.bindings if b.venue != binding.venue]
        self.bindings.append(binding)

    def remove_binding(self, venue: str) -> bool:
        """Remove binding for a venue."""
        original_count = len(self.bindings)
        self.bindings = [b for b in self.bindings if b.venue != venue]
        return len(self.bindings) < original_count

    def has_any_binding(self) -> bool:
        """Check if user has any active bindings."""
        return any(b.is_active for b in self.bindings)

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "telegram_id": self.telegram_id,
            "username": self.username,
            "language": self.language,
            "bindings": [b.to_dict() for b in self.bindings],
            "threshold": self.threshold.to_dict(),
            "is_demo_mode": self.is_demo_mode,
            "created_at": self.created_at.isoformat(),
            "last_active": self.last_active.isoformat(),
        }
