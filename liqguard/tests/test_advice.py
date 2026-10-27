"""Tests for the actionable advice calculator."""

import pytest

from liqguard.models import Position, Side, Venue
from liqguard.risk.advice import ActionableAdvice, ActionType, AdviceCalculator


class TestMarginAdvice:
    """Test margin advice calculations."""
    
    @pytest.fixture
    def long_position_at_risk(self):
        """Create a long position near liquidation."""
        return Position(
            venue=Venue.BINANCE,
            symbol="BTCUSDT",
            side=Side.LONG,
            size=0.5,
            entry_price=65000,
            mark_price=62000,
            liquidation_price=58000,
            margin=3100,
            leverage=10,
            unrealized_pnl=-1500,
        )
    
    @pytest.fixture
    def safe_position(self):
        """Create a safe position."""
        return Position(
            venue=Venue.BINANCE,
            symbol="ETHUSDT",
            side=Side.LONG,
            size=1.0,
            entry_price=3200,
            mark_price=3200,
            liquidation_price=2000,
            margin=3200,
            leverage=1,
            unrealized_pnl=0,
        )
    
    def test_margin_advice_for_risky_position(self, long_position_at_risk):
        """Test that margin advice is calculated for risky position."""
        advice = AdviceCalculator.calculate_margin_to_add(long_position_at_risk)
        
        assert advice.action_type == ActionType.ADD_MARGIN
        assert advice.symbol == "BTCUSDT"
        assert advice.margin_to_add_usd > 0
        assert advice.new_liquidation_price is not None
        assert advice.new_liquidation_price < long_position_at_risk.liquidation_price
    
    @pytest.mark.parametrize("side,liq", [(Side.LONG, 58000), (Side.SHORT, 66000)])
    def test_added_margin_lands_exactly_on_target(self, side, liq):
        """New liquidation price after adding the advised margin sits at the target distance."""
        pos = Position(
            venue=Venue.BINANCE, symbol="BTCUSDT", side=side, size=0.5,
            entry_price=62000, mark_price=62000, liquidation_price=liq,
            margin=3100, leverage=10, unrealized_pnl=0,
        )
        advice = AdviceCalculator.calculate_margin_to_add(pos, 30)

        moved = Position(
            venue=pos.venue, symbol=pos.symbol, side=side, size=pos.size,
            entry_price=pos.entry_price, mark_price=pos.mark_price,
            liquidation_price=advice.new_liquidation_price,
            margin=pos.margin + advice.margin_to_add_usd, leverage=pos.leverage,
            unrealized_pnl=0,
        )
        assert moved.distance_to_liquidation_pct == pytest.approx(30.0)
        assert advice.margin_to_add_usd == pytest.approx(
            abs(pos.liquidation_price - advice.new_liquidation_price) * pos.size
        )

    def test_margin_advice_for_safe_position(self, safe_position):
        """Test that no margin advice for already safe position."""
        advice = AdviceCalculator.calculate_margin_to_add(safe_position)
        
        assert advice.action_type == ActionType.ADD_MARGIN
        assert advice.margin_to_add_usd == 0
    
    def test_margin_advice_with_custom_target(self, long_position_at_risk):
        """Test margin advice with custom target distance."""
        advice_30 = AdviceCalculator.calculate_margin_to_add(long_position_at_risk, 30)
        advice_50 = AdviceCalculator.calculate_margin_to_add(long_position_at_risk, 50)
        
        assert advice_50.margin_to_add_usd > advice_30.margin_to_add_usd
    
    def test_advice_to_dict(self, long_position_at_risk):
        """Test that advice can be serialized to dict."""
        advice = AdviceCalculator.calculate_margin_to_add(long_position_at_risk)
        d = advice.to_dict()
        
        assert "action_type" in d
        assert "symbol" in d
        assert "margin_to_add_usd" in d
        assert d["action_type"] == "add_margin"


class TestReductionAdvice:
    """Test position reduction advice calculations."""
    
    @pytest.fixture
    def position_needing_reduction(self):
        """Create a position that needs reduction."""
        return Position(
            venue=Venue.BINANCE,
            symbol="SOLUSDT",
            side=Side.LONG,
            size=100,
            entry_price=150,
            mark_price=135,
            liquidation_price=125,
            margin=3000,
            leverage=5,
            unrealized_pnl=-1500,
        )
    
    def test_reduction_advice_calculated(self, position_needing_reduction):
        """Test that reduction advice is calculated."""
        advice = AdviceCalculator.calculate_position_reduction(position_needing_reduction)
        
        assert advice.action_type == ActionType.REDUCE_POSITION
        assert advice.reduce_size_by > 0
        assert advice.reduce_size_pct > 0
        assert advice.reduce_size_pct <= 100
    
    def test_no_reduction_for_safe_position(self):
        """Test no reduction advice for safe position."""
        safe = Position(
            venue=Venue.BINANCE,
            symbol="BTCUSDT",
            side=Side.LONG,
            size=0.1,
            entry_price=65000,
            mark_price=65000,
            liquidation_price=40000,
            margin=6500,
            leverage=1,
            unrealized_pnl=0,
        )
        
        advice = AdviceCalculator.calculate_position_reduction(safe)
        assert advice.reduce_size_by == 0


class TestAllOptions:
    """Test getting all advice options."""
    
    def test_all_options_for_risky_position(self):
        """Test that multiple options are returned for risky position."""
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
        
        options = AdviceCalculator.calculate_all_options(position)
        
        assert len(options) >= 1
        action_types = [o.action_type for o in options]
        assert ActionType.ADD_MARGIN in action_types


class TestFormatAdviceText:
    """Test advice text formatting."""
    
    @pytest.fixture
    def margin_advice(self):
        return ActionableAdvice(
            action_type=ActionType.ADD_MARGIN,
            symbol="BTCUSDT",
            venue="binance",
            current_distance_pct=8.5,
            target_distance_pct=30.0,
            margin_to_add_usd=1500.0,
            new_liquidation_price=52000.0,
            new_distance_pct=30.0,
        )
    
    def test_format_english(self, margin_advice):
        """Test English formatting."""
        text = AdviceCalculator.format_advice_text(margin_advice, "en")
        
        assert "BTCUSDT" in text or "$1,500" in text
        assert "Add" in text or "margin" in text.lower()
    
    def test_format_chinese(self, margin_advice):
        """Test Chinese formatting."""
        text = AdviceCalculator.format_advice_text(margin_advice, "zh-TW")
        
        assert "增加" in text or "保證金" in text
