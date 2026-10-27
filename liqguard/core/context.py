"""Application wiring shared by the Telegram bot and the web dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from liqguard.ai import MetaLLMClient
from liqguard.alerts import AlertManager
from liqguard.auth import LoginLinkService, SessionSigner
from liqguard.config import Settings
from liqguard.protect import AuditLogger, ExecutionTier, ProtectiveActionConfig, ProtectiveActionsManager
from liqguard.simulation import MarketSimulation, SimulationRegistry
from liqguard.storage import EncryptedStorage

from .executor import BindingActionExecutor
from .positions import PositionService


@dataclass
class AppContext:
    settings: Settings
    storage: EncryptedStorage
    llm: MetaLLMClient
    alerts: AlertManager
    actions: ProtectiveActionsManager
    simulations: SimulationRegistry
    positions: PositionService
    sessions: SessionSigner
    login_links: LoginLinkService

    @classmethod
    async def create(
        cls,
        settings: Settings,
        storage: Optional[EncryptedStorage] = None,
        llm: Optional[MetaLLMClient] = None,
        simulation_options: Optional[dict] = None,
    ) -> "AppContext":
        storage = storage or EncryptedStorage(db_path=settings.db_path, key_file_path=settings.key_file)
        await storage.initialize()
        llm = llm or MetaLLMClient(api_key=settings.meta_api_key)

        actions = ProtectiveActionsManager(
            config=ProtectiveActionConfig(
                enabled=settings.protect_enabled,
                tier=ExecutionTier.TIER_1_CONFIRM if settings.protect_enabled else ExecutionTier.TIER_0_READONLY,
            ),
            audit=AuditLogger(sink=storage.audit_sink()),
            executor=BindingActionExecutor(storage),
        )

        options = simulation_options or {}

        def make_simulation(owner_id: Optional[int]) -> MarketSimulation:
            return MarketSimulation(llm, owner_id=owner_id, **options)

        simulations = SimulationRegistry(make_simulation)
        simulations.on_create(lambda sim: actions.audit.subscribe(sim.owner_id, sim.record_protective_audit))

        if settings.session_secret:
            secret = settings.session_secret.encode()
            if len(secret) < 32:
                raise ValueError("LIQGUARD_SESSION_SECRET must be at least 32 characters")
        else:
            secret = storage.keys.derive_secret("dashboard-session")

        return cls(
            settings=settings,
            storage=storage,
            llm=llm,
            alerts=AlertManager(),
            actions=actions,
            simulations=simulations,
            positions=PositionService(simulations),
            sessions=SessionSigner(secret, settings.session_ttl),
            login_links=LoginLinkService(storage.login_tokens, settings.login_link_ttl),
        )

    def dashboard_login_url(self, raw_token: str) -> str:
        return f"{self.settings.dashboard_url}/auth/login?token={raw_token}"

    async def close(self) -> None:
        await self.simulations.stop_all()
        await self.storage.close()
