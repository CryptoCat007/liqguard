"""Tests for the Meta LLM client mock mode."""

import pytest

from liqguard.llm import MetaLLMClient
from liqguard.models import AlertSeverity, Position, Side, Venue
from liqguard.services.advice import AdviceCalculator


class TestLLMClientMockMode:
    """Test LLM client in mock mode."""
    
    @pytest.fixture
    def mock_client(self):
        """Create a client in mock mode."""
        return MetaLLMClient(use_mock=True)
    
    @pytest.fixture
    def position(self):
        """Create a test position."""
        return Position(
            venue=Venue.BINANCE,
            symbol="BTCUSDT",
            side=Side.LONG,
            size=0.5,
            entry_price=65000,
            mark_price=60000,
            liquidation_price=55000,
            margin=3000,
            leverage=10,
            unrealized_pnl=-2500,
        )
    
    def test_mock_mode_enabled(self, mock_client):
        """Test that mock mode is properly enabled."""
        assert mock_client.is_mock
        assert mock_client.is_available
        assert mock_client.client is None
    
    def test_auto_mock_without_api_key(self, monkeypatch):
        """Test that mock mode is auto-enabled without API key."""
        monkeypatch.delenv("META_API_KEY", raising=False)
        
        client = MetaLLMClient(api_key=None)
        
        assert client.is_mock
    
    def test_live_mode_with_api_key(self, monkeypatch):
        """Test that live mode is used with API key."""
        client = MetaLLMClient(api_key="test-key-12345")
        
        assert not client.is_mock
        assert client.client is not None
    
    @pytest.mark.asyncio
    async def test_mock_explain_advice(self, mock_client, position):
        """Test mock explanation generation."""
        advice = AdviceCalculator.calculate_margin_to_add(position)
        
        result = await mock_client.explain_risk_with_advice(position, advice, "en")
        
        assert "severity" in result
        assert "summary" in result
        assert "explanation" in result
        assert "advice" in result
        assert result.get("mock") is True
    
    @pytest.mark.asyncio
    async def test_mock_explain_chinese(self, mock_client, position):
        """Test mock explanation in Chinese."""
        advice = AdviceCalculator.calculate_margin_to_add(position)
        
        result = await mock_client.explain_risk_with_advice(position, advice, "zh-TW")
        
        assert "severity" in result
    
    @pytest.mark.asyncio
    async def test_mock_parse_screenshot(self, mock_client):
        """Test mock screenshot parsing."""
        result = await mock_client.parse_screenshot(b"fake image data")
        
        assert result is not None
        assert "symbol" in result
        assert "side" in result
        assert "size" in result
        assert result.get("mock") is True
    
    @pytest.mark.asyncio
    async def test_mock_answer_question(self, mock_client, position):
        """Test mock question answering."""
        answer = await mock_client.answer_portfolio_question(
            [position],
            "What is my risk?",
            "en"
        )
        
        assert isinstance(answer, str)
        assert len(answer) > 0
    
    @pytest.mark.asyncio
    async def test_mock_answer_no_positions(self, mock_client):
        """Test mock answer with no positions."""
        answer = await mock_client.answer_portfolio_question(
            [],
            "What is my risk?",
            "en"
        )
        
        assert "no positions" in answer.lower() or "connect" in answer.lower()
    
    @pytest.mark.asyncio
    async def test_mock_severity_levels(self, mock_client):
        """Test that mock correctly assigns severity based on distance."""
        positions_and_expected = [
            (3.0, "emergency"),
            (8.0, "critical"),
            (15.0, "warning"),
            (35.0, "safe"),
        ]
        
        for distance_approx, expected_severity in positions_and_expected:
            if expected_severity == "emergency":
                liq_price = 58000
                mark_price = 58500
            elif expected_severity == "critical":
                liq_price = 55000
                mark_price = 59000
            elif expected_severity == "warning":
                liq_price = 55000
                mark_price = 64000
            else:
                liq_price = 40000
                mark_price = 65000
            
            pos = Position(
                venue=Venue.BINANCE,
                symbol="BTCUSDT",
                side=Side.LONG,
                size=0.5,
                entry_price=65000,
                mark_price=mark_price,
                liquidation_price=liq_price,
                margin=3000,
                leverage=10,
                unrealized_pnl=0,
            )
            
            advice = AdviceCalculator.calculate_margin_to_add(pos)
            result = await mock_client.explain_risk_with_advice(pos, advice, "en")
            
            assert result["severity"] == expected_severity, f"Expected {expected_severity} for distance ~{distance_approx}%"


class TestLLMClientModel:
    """Test model configuration."""
    
    def test_default_model(self):
        """Test default model is used."""
        client = MetaLLMClient(use_mock=True)
        assert client.model == "muse-spark"
    
    def test_custom_model(self):
        """Test custom model can be set."""
        client = MetaLLMClient(model="custom-model", use_mock=True)
        assert client.model == "custom-model"
    
    def test_model_from_env(self, monkeypatch):
        """Test model can be set from environment."""
        monkeypatch.setenv("META_MODEL", "env-model")
        
        client = MetaLLMClient(use_mock=True)
        assert client.model == "env-model"
