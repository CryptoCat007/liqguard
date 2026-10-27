"""User model for storing user data and bindings."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .alert import UserThreshold

MAINNET = "mainnet"
TESTNET = "testnet"


def mask_secret(value: Optional[str], visible: int = 4) -> Optional[str]:
    """`…ABCD`-style hint that is safe to show in chats and logs."""
    if not value:
        return None
    return "…" + value[-visible:] if len(value) > visible * 2 else "…"


def mask_wallet(address: Optional[str]) -> Optional[str]:
    """`0x1234…abcd`: enough for the owner to recognise, not a full identifier."""
    if not address:
        return None
    return f"{address[:6]}…{address[-4:]}" if len(address) > 12 else "…"


@dataclass
class ExchangeBinding:
    """
    Binding to an exchange (CEX API key or DEX wallet address).

    `api_key` / `api_secret` hold plaintext only in memory after the owning
    user's data key decrypted them; they are excluded from repr so they never
    end up in logs or tracebacks.
    """

    venue: str
    binding_type: str  # "api_key" or "wallet"

    api_key: Optional[str] = field(default=None, repr=False)
    api_secret: Optional[str] = field(default=None, repr=False)

    wallet_address: Optional[str] = None
    network: str = MAINNET

    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_active: bool = True
    last_used: Optional[datetime] = None

    permissions: list[str] = field(default_factory=list)

    @property
    def api_key_hint(self) -> Optional[str]:
        return mask_secret(self.api_key)

    @property
    def is_testnet(self) -> bool:
        return self.network == TESTNET

    def to_dict(self) -> dict:
        """Convert to dictionary (without secrets)."""
        return {
            "venue": self.venue,
            "network": self.network,
            "binding_type": self.binding_type,
            "api_key": self.api_key_hint,
            "wallet_address": mask_wallet(self.wallet_address),
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
    session_epoch: int = 0

    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_active: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def get_binding(self, venue: str, network: str = MAINNET) -> Optional[ExchangeBinding]:
        """Get active binding for a venue on a network."""
        for binding in self.bindings:
            if binding.venue == venue and binding.network == network and binding.is_active:
                return binding
        return None

    def add_binding(self, binding: ExchangeBinding) -> None:
        """Add or replace a binding for a venue/network."""
        self.bindings = [
            b for b in self.bindings
            if (b.venue, b.network) != (binding.venue, binding.network)
        ]
        self.bindings.append(binding)

    def remove_binding(self, venue: str, network: Optional[str] = None) -> bool:
        """Remove binding(s) for a venue (optionally only one network)."""
        original_count = len(self.bindings)
        self.bindings = [
            b for b in self.bindings
            if not (b.venue == venue and (network is None or b.network == network))
        ]
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
