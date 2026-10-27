"""Module boundaries: business logic is framework-free and the package has no import cycles."""

import ast
import logging
from pathlib import Path

import pytest

from liqguard.logsafe import REDACTED, RedactingFilter, redact

PACKAGE = Path(__file__).resolve().parents[1]
FRAMEWORKS = {"aiogram", "fastapi", "starlette", "uvicorn"}
CORE_PACKAGES = ["models", "risk", "ai", "exchanges", "alerts", "protect", "simulation", "storage", "auth", "core"]


def module_name(path: Path) -> str:
    parts = path.relative_to(PACKAGE.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    package = module_name(path) if path.name == "__init__.py" else module_name(path).rsplit(".", 1)[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)]
                found.add(".".join(base + ([node.module] if node.module else [])))
            elif node.module:
                found.add(node.module)
    return found


def source_files(subpackage: str = "") -> list[Path]:
    root = PACKAGE / subpackage if subpackage else PACKAGE
    return [p for p in root.rglob("*.py") if "tests" not in p.parts]


def top_level_subpackage(module: str) -> str:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 and parts[0] == "liqguard" else ""


@pytest.mark.parametrize("subpackage", CORE_PACKAGES)
def test_business_logic_does_not_import_frameworks(subpackage):
    offenders = {
        f"{path.relative_to(PACKAGE)} -> {name}"
        for path in source_files(subpackage)
        for name in imports_of(path)
        if name.split(".")[0] in FRAMEWORKS
    }
    assert not offenders


@pytest.mark.parametrize("module", ["config.py", "logsafe.py"])
def test_top_level_helpers_are_framework_free(module):
    assert not {n for n in imports_of(PACKAGE / module) if n.split(".")[0] in FRAMEWORKS}


@pytest.mark.parametrize("subpackage", CORE_PACKAGES)
def test_business_logic_does_not_import_delivery_layers(subpackage):
    offenders = {
        name for path in source_files(subpackage) for name in imports_of(path)
        if top_level_subpackage(name) in {"telegram", "web"}
    }
    assert not offenders


def test_telegram_and_web_are_independent():
    for a, b in (("telegram", "web"), ("web", "telegram")):
        assert not {n for p in source_files(a) for n in imports_of(p) if top_level_subpackage(n) == b}


def test_no_import_cycles_between_subpackages():
    graph: dict[str, set[str]] = {}
    for path in source_files():
        src = top_level_subpackage(module_name(path))
        if not src:
            continue
        for name in imports_of(path):
            dst = top_level_subpackage(name)
            if dst and dst != src and (PACKAGE / dst).is_dir():
                graph.setdefault(src, set()).add(dst)

    visiting, done = set(), set()

    def visit(node: str, trail: list[str]) -> None:
        if node in done:
            return
        assert node not in visiting, "import cycle: " + " -> ".join(trail + [node])
        visiting.add(node)
        for nxt in graph.get(node, ()):
            visit(nxt, trail + [node])
        visiting.discard(node)
        done.add(node)

    for node in list(graph):
        visit(node, [])


def test_every_module_imports_cleanly():
    import importlib

    for path in source_files():
        importlib.import_module(module_name(path))


# --------------------------------------------------------------------------- log safety


class TestLogRedaction:
    def test_known_secret_shapes_are_redacted(self):
        text = (
            "token 123456789:AAH-abcdefghijklmnopqrstuvwxyz01234 "
            f"key {'a1' * 32} sk-abcdefghijklmnopqrstu "
            "url /auth/login?token=raw-login-token&x=1 "
            "gAAAAABlAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        )
        cleaned = redact(text)
        for secret in ("AAH-abcdef", "a1a1a1a1", "sk-abc", "raw-login-token", "gAAAAABl"):
            assert secret not in cleaned
        assert cleaned.count(REDACTED) == 5

    def test_filter_redacts_args_and_literals(self):
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "binding %s with %s", ("x", "my-wallet-secret"), None)
        RedactingFilter(["my-wallet-secret"]).filter(record)
        assert "my-wallet-secret" not in record.getMessage()

    def test_filter_redacts_exception_text(self):
        try:
            raise RuntimeError("failed with signature=deadbeef")
        except RuntimeError:
            import sys

            record = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", (), sys.exc_info())
        RedactingFilter().filter(record)
        assert "deadbeef" not in (record.exc_text or "")


def test_startup_key_message_is_not_contradictory(tmp_path, monkeypatch):
    monkeypatch.delenv("LIQGUARD_MASTER_KEY", raising=False)
    monkeypatch.delenv("LIQGUARD_ENCRYPTION_KEY", raising=False)
    from liqguard.main import describe_key_source
    from liqguard.storage import EncryptedStorage

    storage = EncryptedStorage(db_path=str(tmp_path / "x.db"), key_file_path=tmp_path / "k.key")
    level, message = describe_key_source(storage)
    assert "ephemeral" not in message.lower()
    assert str(tmp_path / "k.key") in message and "persists" in message
    assert level == logging.WARNING

    level, message = describe_key_source(EncryptedStorage(db_path=str(tmp_path / "y.db"), key_file_path=tmp_path / "k.key"))
    assert level == logging.INFO and "loaded from" in message
