"""Unit tests for the risk engine liquidation calculations."""

import pytest

from liqguard.models import AlertSeverity, Position, Side, UserThreshold, Venue
from liqguard.risk.engine import RiskEngine


class TestDistanceToLiquidation:
    """Test distance to liquidation calculations."""

    def test_long_position_distance(self):
        """Test distance calculation for long position."""
        distance = RiskEngine.calculate_distance_to_liquidation(
            mark_price=65000,
            liquidation_price=60000,
            side=Side.LONG
        )
        expected = ((65000 - 60000) / 65000) * 100
        assert abs(distance - expected) < 0.001
        assert abs(distance - 7.69) < 0.1

    def test_short_position_distance(self):
        """Test distance calculation for short position."""
        distance = RiskEngine.calculate_distance_to_liquidation(
            mark_price=3200,
            liquidation_price=3500,
            side=Side.SHORT
        )
        expected = ((3500 - 3200) / 3200) * 100
        assert abs(distance - expected) < 0.001
        assert abs(distance - 9.38) < 0.1

    def test_long_near_liquidation(self):
        """Test long position very close to liquidation."""
        distance = RiskEngine.calculate_distance_to_liquidation(
            mark_price=60500,
            liquidation_price=60000,
            side=Side.LONG
        )
        assert distance < 1.0
        assert distance > 0

    def test_short_near_liquidation(self):
        """Test short position very close to liquidation."""
        distance = RiskEngine.calculate_distance_to_liquidation(
            mark_price=3480,
            liquidation_price=3500,
            side=Side.SHORT
        )
        assert distance < 1.0
        assert distance > 0

    def test_long_past_liquidation(self):
        """Test long position past liquidation price."""
        distance = RiskEngine.calculate_distance_to_liquidation(
            mark_price=59000,
            liquidation_price=60000,
            side=Side.LONG
        )
        assert distance < 0

    def test_short_past_liquidation(self):
        """Test short position past liquidation price."""
        distance = RiskEngine.calculate_distance_to_liquidation(
            mark_price=3550,
            liquidation_price=3500,
            side=Side.SHORT
        )
        assert distance < 0

    def test_zero_liquidation_price(self):
        """Test with zero liquidation price returns infinity."""
        distance = RiskEngine.calculate_distance_to_liquidation(
            mark_price=65000,
            liquidation_price=0,
            side=Side.LONG
        )
        assert distance == float('inf')

    def test_zero_mark_price(self):
        """Test with zero mark price returns infinity."""
        distance = RiskEngine.calculate_distance_to_liquidation(
            mark_price=0,
            liquidation_price=60000,
            side=Side.LONG
        )
        assert distance == float('inf')


class TestMarginCalculations:
    """Test margin-to-safety calculations."""

    @pytest.fixture
    def long_position(self):
        """Create a long position for testing."""
        return Position(
            venue=Venue.BINANCE,
            symbol="BTCUSDT",
            side=Side.LONG,
            size=0.5,
            entry_price=65000,
            mark_price=64000,
            liquidation_price=60000,
            margin=3250,
            leverage=10,
            unrealized_pnl=-500,
        )

    @pytest.fixture
    def short_position(self):
        """Create a short position for testing."""
        return Position(
            venue=Venue.BINANCE,
            symbol="ETHUSDT",
            side=Side.SHORT,
            size=5.0,
            entry_price=3200,
            mark_price=3300,
            liquidation_price=3500,
            margin=800,
            leverage=20,
            unrealized_pnl=-500,
        )

    def test_margin_already_safe(self, long_position):
        """Test when position is already at target distance."""
        long_position.liquidation_price = 40000

        margin = RiskEngine.calculate_margin_to_reach_distance(
            long_position, target_distance_pct=30
        )
        assert margin == 0.0

    def test_margin_needed_long(self, long_position):
        """Test margin calculation for long position."""
        margin = RiskEngine.calculate_margin_to_reach_distance(
            long_position, target_distance_pct=30
        )
        assert margin > 0

        current_distance = long_position.distance_to_liquidation_pct
        assert current_distance < 30

    def test_margin_needed_short(self, short_position):
        """Test margin calculation for short position."""
        margin = RiskEngine.calculate_margin_to_reach_distance(
            short_position, target_distance_pct=30
        )
        assert margin > 0

        current_distance = short_position.distance_to_liquidation_pct
        assert current_distance < 30

    def test_position_distance_property(self, long_position):
        """Test the Position.distance_to_liquidation_pct property."""
        distance = long_position.distance_to_liquidation_pct

        expected = RiskEngine.calculate_distance_to_liquidation(
            long_position.mark_price,
            long_position.liquidation_price,
            long_position.side
        )

        assert abs(distance - expected) < 0.001


class TestRiskAssessment:
    """Test full risk assessment."""

    @pytest.fixture
    def risk_engine(self):
        """Create risk engine with default thresholds."""
        return RiskEngine()

    @pytest.fixture
    def custom_threshold(self):
        """Create custom thresholds."""
        return UserThreshold(
            warning_distance_pct=25,
            critical_distance_pct=15,
            emergency_distance_pct=7,
            safety_target_pct=35,
        )

    @pytest.fixture
    def safe_position(self):
        """Create a safe position (>30% from liquidation)."""
        return Position(
            venue=Venue.BINANCE,
            symbol="BTCUSDT",
            side=Side.LONG,
            size=0.1,
            entry_price=65000,
            mark_price=65000,
            liquidation_price=40000,  # ~38% distance - safely above 30% target
            margin=6500,
            leverage=1,
            unrealized_pnl=0,
        )

    @pytest.fixture
    def warning_position(self):
        """Create a position at warning level."""
        return Position(
            venue=Venue.BINANCE,
            symbol="BTCUSDT",
            side=Side.LONG,
            size=0.5,
            entry_price=65000,
            mark_price=62000,
            liquidation_price=55000,
            margin=3100,
            leverage=10,
            unrealized_pnl=-1500,
        )

    @pytest.fixture
    def critical_position(self):
        """Create a position at critical level."""
        return Position(
            venue=Venue.BINANCE,
            symbol="BTCUSDT",
            side=Side.LONG,
            size=0.5,
            entry_price=65000,
            mark_price=58000,
            liquidation_price=55000,
            margin=2900,
            leverage=10,
            unrealized_pnl=-3500,
        )

    @pytest.fixture
    def emergency_position(self):
        """Create a position at emergency level."""
        return Position(
            venue=Venue.BINANCE,
            symbol="BTCUSDT",
            side=Side.LONG,
            size=0.5,
            entry_price=65000,
            mark_price=55800,
            liquidation_price=55000,
            margin=2790,
            leverage=10,
            unrealized_pnl=-4600,
        )

    def test_safe_position_assessment(self, risk_engine, safe_position):
        """Test assessment of safe position."""
        assessment = risk_engine.assess_position(safe_position)

        assert assessment.severity is None
        assert not assessment.is_at_risk
        assert not assessment.is_critical
        assert not assessment.is_emergency
        assert assessment.margin_to_safety == 0
        assert assessment.risk_level_str == "SAFE"

    def test_warning_position_assessment(self, risk_engine, warning_position):
        """Test assessment of warning-level position."""
        assessment = risk_engine.assess_position(warning_position)

        assert assessment.severity == AlertSeverity.WARNING
        assert assessment.is_at_risk
        assert not assessment.is_critical
        assert not assessment.is_emergency
        assert assessment.risk_level_str == "WARNING"

    def test_critical_position_assessment(self, risk_engine, critical_position):
        """Test assessment of critical-level position."""
        assessment = risk_engine.assess_position(critical_position)

        assert assessment.severity == AlertSeverity.CRITICAL
        assert assessment.is_at_risk
        assert assessment.is_critical
        assert not assessment.is_emergency
        assert assessment.risk_level_str == "CRITICAL"

    def test_emergency_position_assessment(self, risk_engine, emergency_position):
        """Test assessment of emergency-level position."""
        assessment = risk_engine.assess_position(emergency_position)

        assert assessment.severity == AlertSeverity.EMERGENCY
        assert assessment.is_at_risk
        assert assessment.is_critical
        assert assessment.is_emergency
        assert assessment.risk_level_str == "EMERGENCY"

    def test_custom_thresholds(self, custom_threshold, warning_position):
        """Test with custom thresholds."""
        engine = RiskEngine(custom_threshold)
        assessment = engine.assess_position(warning_position)

        # With custom thresholds (critical=15%), position at ~11% distance is critical
        assert assessment.severity == AlertSeverity.CRITICAL
        assert assessment.is_critical

    def test_multiple_positions_sorted(self, risk_engine, safe_position, warning_position, critical_position):
        """Test that multiple positions are sorted by risk."""
        positions = [safe_position, warning_position, critical_position]
        assessments = risk_engine.assess_positions(positions)

        assert len(assessments) == 3

        assert assessments[0].risk_score >= assessments[1].risk_score
        assert assessments[1].risk_score >= assessments[2].risk_score


class TestAlertCreation:
    """Test alert creation from risk assessments."""

    @pytest.fixture
    def risk_engine(self):
        return RiskEngine()

    def test_create_alert_for_warning(self, risk_engine):
        """Test alert creation for warning-level position."""
        position = Position(
            venue=Venue.BINANCE,
            symbol="BTCUSDT",
            side=Side.LONG,
            size=0.5,
            entry_price=65000,
            mark_price=62000,
            liquidation_price=55000,
            margin=3100,
            leverage=10,
            unrealized_pnl=-1500,
        )

        assessment = risk_engine.assess_position(position)
        alert = risk_engine.create_alert(assessment)

        assert alert is not None
        assert alert.severity == AlertSeverity.WARNING
        assert "BTCUSDT" in alert.symbol
        assert "⚠️" in alert.message

    def test_no_alert_for_safe_position(self, risk_engine):
        """Test that no alert is created for safe position."""
        position = Position(
            venue=Venue.BINANCE,
            symbol="BTCUSDT",
            side=Side.LONG,
            size=0.1,
            entry_price=65000,
            mark_price=65000,
            liquidation_price=50000,
            margin=6500,
            leverage=1,
            unrealized_pnl=0,
        )

        assessment = risk_engine.assess_position(position)
        alert = risk_engine.create_alert(assessment)

        assert alert is None


class TestRiskScore:
    """Test risk scoring."""

    @pytest.fixture
    def risk_engine(self):
        return RiskEngine()

    def test_high_risk_score(self, risk_engine):
        """Test that near-liquidation gets high risk score."""
        position = Position(
            venue=Venue.BINANCE,
            symbol="BTCUSDT",
            side=Side.LONG,
            size=1.0,
            entry_price=65000,
            mark_price=55500,
            liquidation_price=55000,
            margin=3000,
            leverage=20,
            unrealized_pnl=-9500,
        )

        score = risk_engine.calculate_risk_score(position)
        assert score > 80

    def test_low_risk_score(self, risk_engine):
        """Test that safe position gets low risk score."""
        position = Position(
            venue=Venue.BINANCE,
            symbol="BTCUSDT",
            side=Side.LONG,
            size=0.1,
            entry_price=65000,
            mark_price=65000,
            liquidation_price=30000,
            margin=6500,
            leverage=1,
            unrealized_pnl=0,
        )

        score = risk_engine.calculate_risk_score(position)
        assert score < 20

    def test_leverage_affects_score(self, risk_engine):
        """Test that higher leverage increases risk score."""
        low_leverage_pos = Position(
            venue=Venue.BINANCE,
            symbol="BTCUSDT",
            side=Side.LONG,
            size=1.0,
            entry_price=65000,
            mark_price=60000,
            liquidation_price=55000,
            margin=6500,
            leverage=5,
            unrealized_pnl=-5000,
        )

        high_leverage_pos = Position(
            venue=Venue.BINANCE,
            symbol="BTCUSDT",
            side=Side.LONG,
            size=1.0,
            entry_price=65000,
            mark_price=60000,
            liquidation_price=55000,
            margin=2600,
            leverage=25,
            unrealized_pnl=-5000,
        )

        low_score = risk_engine.calculate_risk_score(low_leverage_pos)
        high_score = risk_engine.calculate_risk_score(high_leverage_pos)

        assert high_score > low_score
