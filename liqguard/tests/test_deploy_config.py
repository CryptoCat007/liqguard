"""Deployment config: data paths from the environment, first-boot master key, audit file, /healthz, Docker files."""

import logging
import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from liqguard.ai import MetaLLMClient
from liqguard.config import Settings
from liqguard.core import AppContext
from liqguard.logsafe import AUDIT_LOGGER, install_audit_log, install_log_redaction
from liqguard.models import ExchangeBinding, User
from liqguard.storage import EncryptedStorage
from liqguard.web import create_app

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "123456789:" + "A" * 35


def env(**extra: str) -> dict[str, str]:
    return {"TELEGRAM_BOT_TOKEN": TOKEN, **extra}


def mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class TestDataPaths:
    def test_without_a_data_dir_the_local_defaults_are_unchanged(self):
        s = Settings.from_env(env())
        assert s.data_dir is None
        assert s.db_path == "liqguard.db"
        assert s.key_file is None
        assert s.audit_log_path is None

    def test_data_dir_holds_the_database_master_key_and_audit_log(self, tmp_path):
        s = Settings.from_env(env(LIQGUARD_DATA_DIR=str(tmp_path / "state")))
        assert s.data_dir == tmp_path / "state"
        assert s.db_path == str(tmp_path / "state" / "liqguard.db")
        assert s.key_file == tmp_path / "state" / "encryption.key"
        assert s.audit_log_path == tmp_path / "state" / "audit.log"

    def test_each_path_overrides_the_data_dir(self, tmp_path):
        s = Settings.from_env(env(
            LIQGUARD_DATA_DIR=str(tmp_path / "state"),
            LIQGUARD_DB_PATH=str(tmp_path / "db" / "main.db"),
            LIQGUARD_KEY_FILE=str(tmp_path / "secrets" / "master.key"),
            LIQGUARD_AUDIT_LOG=str(tmp_path / "logs" / "audit.log"),
        ))
        assert s.db_path == str(tmp_path / "db" / "main.db")
        assert s.key_file == tmp_path / "secrets" / "master.key"
        assert s.audit_log_path == tmp_path / "logs" / "audit.log"

    def test_empty_values_fall_back_to_the_data_dir(self):
        """docker-compose.yml blanks the per-file variables so a copied .env.example can't override them."""
        s = Settings.from_env(env(LIQGUARD_DATA_DIR="/data", LIQGUARD_DB_PATH="", LIQGUARD_KEY_FILE="",
                                  LIQGUARD_AUDIT_LOG=""))
        assert (s.db_path, s.key_file, s.audit_log_path) == (
            "/data/liqguard.db", Path("/data/encryption.key"), Path("/data/audit.log"))

    def test_home_is_expanded(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        s = Settings.from_env(env(LIQGUARD_DATA_DIR="~/lg", LIQGUARD_DB_PATH="~/db/x.db"))
        assert s.data_dir == tmp_path / "lg"
        assert s.db_path == str(tmp_path / "db" / "x.db")
        assert s.key_file == tmp_path / "lg" / "encryption.key"

    def test_ensure_data_dirs_creates_every_parent(self, tmp_path):
        s = Settings.from_env(env(
            LIQGUARD_DATA_DIR=str(tmp_path / "a"),
            LIQGUARD_DB_PATH=str(tmp_path / "b" / "c" / "x.db"),
            LIQGUARD_AUDIT_LOG=str(tmp_path / "d" / "audit.log"),
        ))
        s.ensure_data_dirs()
        for d in ("a", "b/c", "d"):
            assert (tmp_path / d).is_dir()


class TestPort:
    def test_port_env_is_the_fallback(self):
        assert Settings.from_env(env(PORT="10000")).dashboard_port == 10000

    def test_dashboard_port_wins_over_port(self):
        assert Settings.from_env(env(PORT="10000", LIQGUARD_DASHBOARD_PORT="9001")).dashboard_port == 9001

    def test_empty_dashboard_port_uses_port(self):
        assert Settings.from_env(env(PORT="8765", LIQGUARD_DASHBOARD_PORT="")).dashboard_port == 8765

    def test_default_port_and_host(self):
        s = Settings.from_env(env())
        assert (s.dashboard_host, s.dashboard_port) == ("127.0.0.1", 8765)

    def test_container_settings_bind_all_interfaces(self):
        s = Settings.from_env(env(LIQGUARD_DASHBOARD="true", LIQGUARD_DASHBOARD_HOST="0.0.0.0", PORT="8765",
                                  LIQGUARD_PUBLIC_URL="https://liqguard.example.com/"))
        assert (s.dashboard_enabled, s.dashboard_host, s.dashboard_port) == (True, "0.0.0.0", 8765)
        assert s.dashboard_url == "https://liqguard.example.com" and s.secure_cookies


class TestFirstBoot:
    @pytest.fixture(autouse=True)
    def no_master_key_env(self, monkeypatch):
        monkeypatch.delenv("LIQGUARD_MASTER_KEY", raising=False)
        monkeypatch.delenv("LIQGUARD_ENCRYPTION_KEY", raising=False)

    async def boot(self, data_dir: Path) -> AppContext:
        settings = Settings.from_env(env(LIQGUARD_DATA_DIR=str(data_dir)))
        return await AppContext.create(settings, llm=MetaLLMClient(use_mock=True))

    async def test_first_boot_generates_a_private_key_in_the_data_dir_and_reuses_it(self, tmp_path):
        data = tmp_path / "volume" / "data"
        ctx = await self.boot(data)
        try:
            assert ctx.storage.key_source == "file_created"
            key_file = data / "encryption.key"
            assert key_file.is_file() and mode(key_file) == 0o600
            assert ctx.storage.db_path == data / "liqguard.db"
            await ctx.storage.save_user(User(telegram_id=7))
            await ctx.storage.save_binding(7, ExchangeBinding(venue="binance", binding_type="api_key",
                                                              api_key="k" * 64, api_secret="s" * 64))
            first_key = key_file.read_bytes()
        finally:
            await ctx.close()

        ctx = await self.boot(data)
        try:
            assert ctx.storage.key_source == "file"
            assert key_file.read_bytes() == first_key
            binding = (await ctx.storage.get_user(7)).bindings[0]
            assert binding.api_secret == "s" * 64
        finally:
            await ctx.close()

    async def test_master_key_env_wins_and_writes_no_key_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LIQGUARD_MASTER_KEY", EncryptedStorage.generate_key())
        ctx = await self.boot(tmp_path / "data")
        try:
            assert ctx.storage.key_source == "env"
            assert not (tmp_path / "data" / "encryption.key").exists()
        finally:
            await ctx.close()


class TestAuditLogFile:
    @pytest.fixture
    def audit_logger(self):
        logger = logging.getLogger(AUDIT_LOGGER)
        before = list(logger.handlers)
        yield logger
        for handler in logger.handlers[len(before):]:
            logger.removeHandler(handler)
            handler.close()

    def test_audit_lines_go_to_the_file_redacted_and_private(self, tmp_path, audit_logger):
        path = tmp_path / "logs" / "audit.log"
        redaction = install_log_redaction(TOKEN)
        handler = install_audit_log(path, redaction)
        assert install_audit_log(path, redaction) is handler
        audit_logger.info("AUDIT user=%s add_margin executed BTCUSDT via %s", 42, TOKEN)
        handler.flush()
        text = path.read_text()
        assert "AUDIT user=42 add_margin executed BTCUSDT" in text
        assert TOKEN not in text and "[REDACTED]" in text
        assert mode(path) == 0o600
        for root_handler in logging.getLogger().handlers:
            root_handler.removeFilter(redaction)

    def test_no_path_means_no_file(self):
        assert install_audit_log(None) is None


class TestHealthz:
    def test_public_dashboard(self):
        with TestClient(create_app(llm_client=MetaLLMClient(use_mock=True))) as client:
            r = client.get("/healthz")
        assert r.status_code == 200 and r.json() == {"status": "ok"}

    async def test_with_storage_checks_the_database(self, tmp_path):
        settings = Settings(db_path=str(tmp_path / "liqguard.db"), key_file=tmp_path / "k.key")
        ctx = await AppContext.create(settings, llm=MetaLLMClient(use_mock=True))
        try:
            with TestClient(create_app(ctx), base_url="https://testserver") as client:
                assert client.get("/healthz").json() == {"status": "ok"}

                def broken():
                    raise OSError("disk gone")

                ctx.storage.db.connect = broken
                r = client.get("/healthz")
            assert r.status_code == 503 and r.json() == {"status": "error", "database": "unreachable"}
        finally:
            await ctx.close()


class TestDockerFiles:
    def test_dockerfile_keeps_state_in_the_volume_and_binds_all_interfaces(self):
        text = (ROOT / "Dockerfile").read_text()
        assert "LIQGUARD_DATA_DIR=/data" in text
        assert "LIQGUARD_DASHBOARD_HOST=0.0.0.0" in text
        assert "/healthz" in text
        assert "USER liqguard" in text
        copies = [line for line in text.splitlines() if line.startswith(("COPY", "ADD"))]
        assert copies == ["COPY pyproject.toml README.md ./", "COPY liqguard ./liqguard"]

    def test_dockerignore_excludes_secrets_and_state(self):
        lines = (ROOT / ".dockerignore").read_text().splitlines()
        for pattern in (".env", "*.key", "*.db", "*.log", "/data/", ".git"):
            assert pattern in lines

    def test_compose_mounts_a_named_volume_and_pins_the_data_paths(self):
        yaml = pytest.importorskip("yaml")
        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
        app = compose["services"]["liqguard"]
        assert "liqguard-data:/data" in app["volumes"]
        assert "liqguard-data" in compose["volumes"]
        environment = app["environment"]
        assert environment["LIQGUARD_DATA_DIR"] == "/data"
        assert environment["LIQGUARD_DASHBOARD_HOST"] == "0.0.0.0"
        assert all(environment[k] == "" for k in ("LIQGUARD_DB_PATH", "LIQGUARD_KEY_FILE", "LIQGUARD_AUDIT_LOG"))
        assert all(p.startswith("127.0.0.1:") for p in app["ports"])
        assert "deploy" not in app and "scale" not in app
        assert compose["services"]["caddy"]["profiles"] == ["https"]

    def test_no_state_or_secret_file_is_tracked(self):
        import subprocess

        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
        assert not [f for f in tracked if f.endswith((".key", ".db", ".pem")) or f == ".env" or f.startswith("data/")]
