"""Tests for the portfolio analyzer."""

import pytest

from liqguard.models import Position, Side, UserThreshold, Venue
from liqguard.services.portfolio import (
    PortfolioAnalyzer,
    PortfolioRiskOverview,
    RiskLevel,
    format_portfolio_overview_text,
)


class TestPortfolioAnalyzer:
    """Test portfolio analysis."""
    
    @pytest.fixture
    def analyzer(self):
        return PortfolioAnalyzer()
    
    @pytest.fixture
    def mixed_positions(self):
        """Create a mix of safe and risky positions."""
        return [
            Position(
                venue=Venue.BINANCE,
                symbol="BTCUSDT",
                side=Side.LONG,
                size=0.5,
                entry_price=65000,
                mark_price=65000,
                liquidation_price=50000,
                margin=3250,
                leverage=10,
                unrealized_pnl=0,
            ),
            Position(
                venue=Venue.BINANCE,
                symbol="ETHUSDT",
                side=Side.SHORT,
                size=5.0,
                entry_price=3200,
                mark_price=3400,
                liquidation_price=3600,
                margin=800,
                leverage=20,
                unrealized_pnl=-1000,
            ),
            Position(
                venue=Venue.HYPERLIQUID,
                symbol="SOL-PERP",
                side=Side.LONG,
                size=100,
                entry_price=150,
                mark_price=145,
                liquidation_price=130,
                margin=3000,
                leverage=5,
                unrealized_pnl=-500,
            ),
        ]
    
    @pytest.fixture
    def safe_positions(self):
        """Create all safe positions."""
        return [
            Position(
                venue=Venue.BINANCE,
                symbol="BTCUSDT",
                side=Side.LONG,
                size=0.1,
                entry_price=65000,
                mark_price=66000,
                liquidation_price=30000,
                margin=6500,
                leverage=1,
                unrealized_pnl=100,
            ),
        ]
    
    @pytest.fixture
    def critical_positions(self):
        """Create positions at critical risk."""
        return [
            Position(
                venue=Venue.BINANCE,
                symbol="BTCUSDT",
                side=Side.LONG,
                size=1.0,
                entry_price=65000,
                mark_price=56000,
                liquidation_price=55000,
                margin=3000,
                leverage=20,
                unrealized_pnl=-9000,
            ),
        ]
    
    def test_analyze_single_position(self, analyzer, safe_positions):
        """Test analyzing a single position."""
        summary = analyzer.analyze_position(safe_positions[0])
        
        assert summary.symbol == "BTCUSDT"
        assert summary.side == "long"
        assert summary.distance_to_liquidation_pct > 30
        assert summary.severity is None
    
    def test_analyze_portfolio_metrics(self, analyzer, mixed_positions):
        """Test portfolio-level metrics."""
        overview = analyzer.analyze_portfolio(mixed_positions)
        
        assert overview.total_positions == 3
        assert overview.total_notional_value > 0
        assert overview.total_margin_used > 0
        assert len(overview.position_summaries) == 3
        assert len(overview.venues) == 2
    
    def test_portfolio_exposure(self, analyzer, mixed_positions):
        """Test exposure calculations."""
        overview = analyzer.analyze_portfolio(mixed_positions)
        
        assert overview.long_exposure > 0
        assert overview.short_exposure > 0
        assert overview.net_exposure == overview.long_exposure - overview.short_exposure
    
    def test_riskiest_position_identified(self, analyzer, mixed_positions):
        """Test that riskiest position is identified."""
        overview = analyzer.analyze_portfolio(mixed_positions)
        
        assert overview.riskiest_position is not None
        
        for summary in overview.position_summaries:
            assert summary.risk_score <= overview.riskiest_position.risk_score
    
    def test_safe_portfolio_risk_level(self, analyzer, safe_positions):
        """Test that safe portfolio has safe risk level."""
        overview = analyzer.analyze_portfolio(safe_positions)
        
        assert overview.overall_risk_level == RiskLevel.SAFE
        assert overview.positions_at_risk == 0
    
    def test_critical_portfolio_risk_level(self, analyzer, critical_positions):
        """Test that critical portfolio has high/critical risk level."""
        overview = analyzer.analyze_portfolio(critical_positions)
        
        assert overview.overall_risk_level in [RiskLevel.HIGH, RiskLevel.CRITICAL]
        assert overview.positions_at_risk > 0
    
    def test_empty_portfolio(self, analyzer):
        """Test empty portfolio analysis."""
        overview = analyzer.analyze_portfolio([])
        
        assert overview.total_positions == 0
        assert overview.overall_risk_level == RiskLevel.SAFE
    
    def test_portfolio_to_dict(self, analyzer, mixed_positions):
        """Test portfolio overview serialization."""
        overview = analyzer.analyze_portfolio(mixed_positions)
        d = overview.to_dict()
        
        assert "summary" in d
        assert "risk_metrics" in d
        assert "exposure" in d
        assert "positions" in d
        assert d["summary"]["total_positions"] == 3
    
    def test_margin_needed_calculated(self, analyzer, mixed_positions):
        """Test that total margin needed is calculated."""
        overview = analyzer.analyze_portfolio(mixed_positions)
        
        assert overview.total_margin_needed_for_safety >= 0


class TestPortfolioWithCustomThresholds:
    """Test portfolio analysis with custom thresholds."""
    
    def test_custom_threshold_affects_risk(self):
        """Test that custom thresholds affect risk assessment."""
        position = Position(
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
        
        strict = UserThreshold(warning_distance_pct=25, critical_distance_pct=15)
        lenient = UserThreshold(warning_distance_pct=5, critical_distance_pct=3)
        
        strict_analyzer = PortfolioAnalyzer(strict)
        lenient_analyzer = PortfolioAnalyzer(lenient)
        
        strict_overview = strict_analyzer.analyze_portfolio([position])
        lenient_overview = lenient_analyzer.analyze_portfolio([position])
        
        assert strict_overview.positions_at_risk >= lenient_overview.positions_at_risk


class TestFormatOverviewText:
    """Test portfolio overview text formatting."""
    
    @pytest.fixture
    def sample_overview(self):
        return PortfolioRiskOverview(
            total_positions=3,
            total_notional_value=50000,
            total_margin_used=5000,
            total_unrealized_pnl=-500,
            positions_at_risk=1,
            positions_critical=0,
            positions_emergency=0,
            overall_risk_level=RiskLevel.MODERATE,
            overall_risk_score=45,
            total_margin_needed_for_safety=1000,
            min_distance_to_liquidation=12.5,
            long_exposure=40000,
            short_exposure=10000,
            net_exposure=30000,
        )
    
    def test_format_english(self, sample_overview):
        """Test English text formatting."""
        text = format_portfolio_overview_text(sample_overview, "en")
        
        assert "Portfolio Risk Overview" in text
        assert "MODERATE" in text
        assert "3" in text
        assert "$50,000" in text or "50000" in text
    
    def test_format_chinese(self, sample_overview):
        """Test Chinese text formatting."""
        text = format_portfolio_overview_text(sample_overview, "zh-TW")
        
        assert "投資組合" in text or "風險" in text
        assert "3" in text


class TestActionableAdvice:
    """Test getting actionable advice from portfolio."""
    
    def test_prioritized_advice(self):
        """Test that advice is prioritized by risk."""
        positions = [
            Position(
                venue=Venue.BINANCE,
                symbol="BTCUSDT",
                side=Side.LONG,
                size=1.0,
                entry_price=65000,
                mark_price=58000,
                liquidation_price=55000,
                margin=3000,
                leverage=20,
                unrealized_pnl=-7000,
            ),
            Position(
                venue=Venue.BINANCE,
                symbol="ETHUSDT",
                side=Side.LONG,
                size=5.0,
                entry_price=3200,
                mark_price=3100,
                liquidation_price=2800,
                margin=800,
                leverage=20,
                unrealized_pnl=-500,
            ),
        ]
        
        analyzer = PortfolioAnalyzer()
        advice_list = analyzer.get_actionable_advice(positions)
        
        assert len(advice_list) > 0
        
        if len(advice_list) > 1:
            assert advice_list[0].current_distance_pct <= advice_list[1].current_distance_pct
