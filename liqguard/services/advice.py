"""Actionable advice calculations - deterministic, no LLM involvement in numbers."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from liqguard.models import Position, Side


class ActionType(str, Enum):
    """Types of protective actions."""
    ADD_MARGIN = "add_margin"
    REDUCE_POSITION = "reduce_position"
    CLOSE_POSITION = "close_position"


@dataclass
class ActionableAdvice:
    """
    Concrete actionable advice with exact numbers.
    
    All calculations are deterministic - LLM only explains these values.
    """
    action_type: ActionType
    symbol: str
    venue: str
    
    current_distance_pct: float
    target_distance_pct: float
    
    margin_to_add_usd: Optional[float] = None
    
    reduce_size_by: Optional[float] = None
    reduce_size_pct: Optional[float] = None
    
    new_liquidation_price: Optional[float] = None
    new_distance_pct: Optional[float] = None
    
    estimated_cost_usd: Optional[float] = None
    
    is_executable: bool = False
    execution_venue: Optional[str] = None
    
    def to_dict(self) -> dict:
        """Convert to dictionary for LLM context."""
        return {
            "action_type": self.action_type.value,
            "symbol": self.symbol,
            "venue": self.venue,
            "current_distance_pct": round(self.current_distance_pct, 2),
            "target_distance_pct": round(self.target_distance_pct, 2),
            "margin_to_add_usd": round(self.margin_to_add_usd, 2) if self.margin_to_add_usd else None,
            "reduce_size_by": round(self.reduce_size_by, 4) if self.reduce_size_by else None,
            "reduce_size_pct": round(self.reduce_size_pct, 2) if self.reduce_size_pct else None,
            "new_liquidation_price": round(self.new_liquidation_price, 2) if self.new_liquidation_price else None,
            "new_distance_pct": round(self.new_distance_pct, 2) if self.new_distance_pct else None,
            "estimated_cost_usd": round(self.estimated_cost_usd, 2) if self.estimated_cost_usd else None,
            "is_executable": self.is_executable,
            "execution_venue": self.execution_venue,
        }


class AdviceCalculator:
    """
    Deterministic calculator for actionable advice.
    
    All numbers are computed here - the LLM is never asked to calculate.
    """
    
    DEFAULT_SAFETY_TARGET = 30.0
    
    @staticmethod
    def calculate_margin_to_add(
        position: Position,
        target_distance_pct: float = DEFAULT_SAFETY_TARGET
    ) -> ActionableAdvice:
        """
        Calculate exact margin to add to reach target distance.
        
        Isolated-margin model: adding M USD moves the liquidation price by M / size.
            distance_gap = target_distance - current_distance
            price_change_needed = (distance_gap / 100) * mark_price
            margin_needed = price_change_needed * size
        """
        current_distance = position.distance_to_liquidation_pct
        
        if current_distance >= target_distance_pct:
            return ActionableAdvice(
                action_type=ActionType.ADD_MARGIN,
                symbol=position.symbol,
                venue=position.venue.value,
                current_distance_pct=current_distance,
                target_distance_pct=target_distance_pct,
                margin_to_add_usd=0.0,
                new_distance_pct=current_distance,
                new_liquidation_price=position.liquidation_price,
            )
        
        distance_gap = target_distance_pct - current_distance
        price_change_needed = (distance_gap / 100) * position.mark_price
        margin_needed = price_change_needed * abs(position.size)
        
        if position.side == Side.LONG:
            new_liq_price = position.liquidation_price - (margin_needed / position.size)
        else:
            new_liq_price = position.liquidation_price + (margin_needed / position.size)
        
        return ActionableAdvice(
            action_type=ActionType.ADD_MARGIN,
            symbol=position.symbol,
            venue=position.venue.value,
            current_distance_pct=current_distance,
            target_distance_pct=target_distance_pct,
            margin_to_add_usd=margin_needed,
            new_liquidation_price=new_liq_price,
            new_distance_pct=target_distance_pct,
            estimated_cost_usd=margin_needed,
        )
    
    @staticmethod
    def calculate_position_reduction(
        position: Position,
        target_distance_pct: float = DEFAULT_SAFETY_TARGET
    ) -> ActionableAdvice:
        """
        Calculate position size reduction to reach target distance.
        
        Reducing position size proportionally affects the liquidation distance.
        """
        current_distance = position.distance_to_liquidation_pct
        
        if current_distance >= target_distance_pct:
            return ActionableAdvice(
                action_type=ActionType.REDUCE_POSITION,
                symbol=position.symbol,
                venue=position.venue.value,
                current_distance_pct=current_distance,
                target_distance_pct=target_distance_pct,
                reduce_size_by=0.0,
                reduce_size_pct=0.0,
                new_distance_pct=current_distance,
            )
        
        if current_distance <= 0:
            return ActionableAdvice(
                action_type=ActionType.CLOSE_POSITION,
                symbol=position.symbol,
                venue=position.venue.value,
                current_distance_pct=current_distance,
                target_distance_pct=target_distance_pct,
                reduce_size_by=position.size,
                reduce_size_pct=100.0,
            )
        
        reduction_factor = 1 - (current_distance / target_distance_pct)
        reduction_factor = min(max(reduction_factor, 0), 1)
        
        reduce_by = position.size * reduction_factor
        reduce_pct = reduction_factor * 100
        
        return ActionableAdvice(
            action_type=ActionType.REDUCE_POSITION,
            symbol=position.symbol,
            venue=position.venue.value,
            current_distance_pct=current_distance,
            target_distance_pct=target_distance_pct,
            reduce_size_by=reduce_by,
            reduce_size_pct=reduce_pct,
            new_distance_pct=target_distance_pct,
        )
    
    @staticmethod
    def calculate_all_options(
        position: Position,
        target_distance_pct: float = DEFAULT_SAFETY_TARGET
    ) -> list[ActionableAdvice]:
        """Calculate all available risk-reducing actions."""
        options = []
        
        margin_advice = AdviceCalculator.calculate_margin_to_add(
            position, target_distance_pct
        )
        if margin_advice.margin_to_add_usd and margin_advice.margin_to_add_usd > 0:
            options.append(margin_advice)
        
        reduction_advice = AdviceCalculator.calculate_position_reduction(
            position, target_distance_pct
        )
        if reduction_advice.reduce_size_by and reduction_advice.reduce_size_by > 0:
            options.append(reduction_advice)
        
        return options
    
    @staticmethod
    def format_advice_text(advice: ActionableAdvice, language: str = "en") -> str:
        """Format advice as human-readable text."""
        if language == "zh-TW":
            if advice.action_type == ActionType.ADD_MARGIN:
                if advice.margin_to_add_usd == 0:
                    return f"✅ {advice.symbol} 倉位目前安全，距離清算 {advice.current_distance_pct:.1f}%"
                return (
                    f"💰 增加 ${advice.margin_to_add_usd:,.2f} USDT 保證金\n"
                    f"   → 清算距離從 {advice.current_distance_pct:.1f}% 提升至 {advice.target_distance_pct:.1f}%\n"
                    f"   → 新清算價格: ${advice.new_liquidation_price:,.2f}"
                )
            elif advice.action_type == ActionType.REDUCE_POSITION:
                if advice.reduce_size_by == 0:
                    return f"✅ {advice.symbol} 倉位大小無需調整"
                return (
                    f"📉 減少 {advice.reduce_size_pct:.1f}% 倉位 ({advice.reduce_size_by:.4f})\n"
                    f"   → 清算距離從 {advice.current_distance_pct:.1f}% 提升至 {advice.target_distance_pct:.1f}%"
                )
            elif advice.action_type == ActionType.CLOSE_POSITION:
                return f"🚫 建議立即平倉 {advice.symbol}，倉位已過度風險"
        else:
            if advice.action_type == ActionType.ADD_MARGIN:
                if advice.margin_to_add_usd == 0:
                    return f"✅ {advice.symbol} position is safe at {advice.current_distance_pct:.1f}% from liquidation"
                return (
                    f"💰 Add ${advice.margin_to_add_usd:,.2f} USDT margin\n"
                    f"   → Distance improves from {advice.current_distance_pct:.1f}% to {advice.target_distance_pct:.1f}%\n"
                    f"   → New liquidation price: ${advice.new_liquidation_price:,.2f}"
                )
            elif advice.action_type == ActionType.REDUCE_POSITION:
                if advice.reduce_size_by == 0:
                    return f"✅ {advice.symbol} position size is adequate"
                return (
                    f"📉 Reduce position by {advice.reduce_size_pct:.1f}% ({advice.reduce_size_by:.4f})\n"
                    f"   → Distance improves from {advice.current_distance_pct:.1f}% to {advice.target_distance_pct:.1f}%"
                )
            elif advice.action_type == ActionType.CLOSE_POSITION:
                return f"🚫 Recommend closing {advice.symbol} immediately - position at extreme risk"
        
        return str(advice.to_dict())
