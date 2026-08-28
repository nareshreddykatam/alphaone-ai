from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import structlog

from services.risk_engine.engine import RiskEngine, RiskConfig
from services.trade_journal.pnl import compute_slice_pnl, compute_r_multiple

logger = structlog.get_logger()

# Fraction of the ORIGINAL quantity closed at each target, keyed by how many
# targets the signal actually supplied. A signal with only TP1 (no TP2/TP3)
# closes 100% there, unchanged from the original single-target behavior --
# partial exits only activate when a signal genuinely provides more than
# one target.
_SLICE_FRACTIONS = {1: (1.0,), 2: (0.5, 0.5), 3: (0.4, 0.3, 0.3)}


@dataclass
class PaperPosition:
    trade_id: str
    signal_id: str
    side: str
    entry_price: float
    entry_time: datetime
    stop_loss: float
    take_profit_1: float
    take_profit_2: Optional[float]
    take_profit_3: Optional[float]
    quantity: float
    leverage: int = 1
    market_regime: str = ""
    strategy_name: str = ""
    remaining_quantity: float = field(init=False)
    _slices: tuple = field(init=False)
    _next_target_idx: int = field(default=0, init=False)
    realized_pnl: float = field(default=0.0, init=False)
    realized_fees: float = field(default=0.0, init=False)

    def __post_init__(self):
        self.remaining_quantity = self.quantity
        targets = [t for t in (self.take_profit_1, self.take_profit_2, self.take_profit_3) if t is not None]
        n = len(targets) if targets else 1
        fractions = _SLICE_FRACTIONS.get(n, _SLICE_FRACTIONS[1])
        self._slices = tuple(self.quantity * f for f in fractions)

    def targets(self) -> list[float]:
        return [t for t in (self.take_profit_1, self.take_profit_2, self.take_profit_3) if t is not None]


class PaperTrader:
    """Simulates the exact same signal -> entry -> SL/TP1/TP2/TP3 lifecycle
    a live execution would follow, using the same PnL math as the manual
    trade journal (services/trade_journal/pnl.py) -- never a separate,
    divergent accounting path. In-memory only (equity/risk state resets on
    process restart, same documented limitation as RiskEngine's own notional
    equity tracker -- see docs/known_limitations.md); durable trade records
    live in the Trade/TradeExecution tables via
    services/paper_trader/persistence.py, which is the source of truth for
    reporting even across a restart.
    """

    def __init__(self, risk_config: RiskConfig | None = None, initial_equity: float = 10000):
        self.risk_engine = RiskEngine(risk_config, initial_equity)
        self.initial_equity = initial_equity
        self.equity = initial_equity
        self.positions: dict[str, PaperPosition] = {}
        self.closed_trades: list[dict] = []
        self.trade_counter = 0
        self.is_paused = False

    def process_candle(self, candle) -> list[dict]:
        """Checks every open position's SL and remaining TP targets against
        one candle's high/low. A stop-loss always closes the ENTIRE
        remaining position (standard risk management -- a partial stop
        makes no sense). A take-profit target closes only that slice's
        quantity; the position stays open (at reduced size) for its
        remaining targets, exactly like the paper-trading equivalent of
        scaling out of a real position."""
        if self.is_paused:
            return []

        events = []
        for trade_id, pos in list(self.positions.items()):
            stop_hit = (candle.low <= pos.stop_loss) if pos.side == "LONG" else (candle.high >= pos.stop_loss)
            if stop_hit:
                events.append(self._close_remaining(pos, pos.stop_loss, candle.timestamp, "stop_loss"))
                continue

            while pos._next_target_idx < len(pos.targets()):
                target = pos.targets()[pos._next_target_idx]
                target_hit = (candle.high >= target) if pos.side == "LONG" else (candle.low <= target)
                if not target_hit:
                    break
                is_last_target = pos._next_target_idx == len(pos.targets()) - 1
                if is_last_target:
                    events.append(self._close_remaining(pos, target, candle.timestamp, "take_profit"))
                    break
                events.append(self._partial_exit(pos, target, candle.timestamp, pos._next_target_idx))
                pos._next_target_idx += 1

        return events

    def open_position(self, signal, current_price: float) -> Optional[PaperPosition]:
        if self.is_paused:
            logger.info("Paper trading paused, skipping")
            return None

        can_trade, reason = self.risk_engine.can_trade()
        if not can_trade:
            logger.warning("Risk check failed", reason=reason)
            return None

        if signal.entry_price is None or signal.stop_loss is None or signal.take_profit_1 is None:
            return None

        leverage = min(1, self.risk_engine.config.max_leverage)
        quantity = self.risk_engine.calculate_position_size(
            signal.entry_price, signal.stop_loss, leverage
        )

        if quantity <= 0:
            return None

        self.trade_counter += 1
        trade_id = f"PAPER-{self.trade_counter:06d}"

        position = PaperPosition(
            trade_id=trade_id,
            signal_id=signal.signal_id,
            side=signal.signal_type,
            entry_price=signal.entry_price,
            entry_time=signal.timestamp,
            stop_loss=signal.stop_loss,
            take_profit_1=signal.take_profit_1,
            take_profit_2=getattr(signal, "take_profit_2", None),
            take_profit_3=getattr(signal, "take_profit_3", None),
            quantity=quantity,
            leverage=leverage,
            market_regime=signal.market_regime,
            strategy_name=getattr(signal, "strategy_name", ""),
        )

        self.positions[trade_id] = position
        self.risk_engine.record_position_open()

        logger.info("Paper position opened",
                    trade_id=trade_id, side=signal.signal_type,
                    entry=signal.entry_price, sl=signal.stop_loss,
                    targets=position.targets(), quantity=quantity)

        return position

    def _partial_exit(self, position: PaperPosition, exit_price: float, timestamp: datetime, target_idx: int) -> dict:
        slice_qty = position._slices[target_idx]
        slice_pnl = compute_slice_pnl(position.side, position.entry_price, exit_price, slice_qty, position.leverage)
        position.remaining_quantity -= slice_qty
        position.realized_pnl += slice_pnl.pnl
        position.realized_fees += slice_pnl.fees
        self.equity += slice_pnl.pnl

        result = {
            "event_type": "partial_exit",
            "trade_id": position.trade_id,
            "signal_id": position.signal_id,
            "side": position.side,
            "exit_price": exit_price,
            "exit_time": timestamp,
            "quantity": slice_qty,
            "target_index": target_idx + 1,  # 1-based: which TP this was
            "pnl": round(slice_pnl.pnl, 2),
            "fees": round(slice_pnl.fees, 2),
            "equity_after": round(self.equity, 2),
        }
        logger.info("Paper partial exit", **{k: v for k, v in result.items() if k != "event_type"})
        return result

    def _close_remaining(
        self, position: PaperPosition, exit_price: float, timestamp: datetime, reason: str
    ) -> dict:
        qty = position.remaining_quantity
        slice_pnl = compute_slice_pnl(position.side, position.entry_price, exit_price, qty, position.leverage)
        self.equity += slice_pnl.pnl

        # TRADE-level totals across every slice (earlier partial exits plus
        # this final one) -- required so a partially-profitable trade whose
        # last slice stops out small isn't misreported (or mis-fed into the
        # risk engine's consecutive-loss counter) as a net loss when the
        # trade as a whole was profitable.
        total_pnl = position.realized_pnl + slice_pnl.pnl
        total_fees = position.realized_fees + slice_pnl.fees
        original_notional = position.entry_price * position.quantity
        total_pnl_pct = (total_pnl / original_notional) * 100 if original_notional > 0 else 0.0
        r_multiple = compute_r_multiple(position.entry_price, position.stop_loss, position.quantity, total_pnl)

        self.risk_engine.record_trade_result(total_pnl_pct)
        self.risk_engine.record_position_close()

        del self.positions[position.trade_id]

        result = {
            "event_type": "exit",
            "trade_id": position.trade_id,
            "signal_id": position.signal_id,
            "side": position.side,
            "entry_price": position.entry_price,
            "exit_price": exit_price,
            "entry_time": position.entry_time,
            "exit_time": timestamp,
            "stop_loss": position.stop_loss,
            "take_profit": position.take_profit_1,
            "quantity": qty,
            "leverage": position.leverage,
            "pnl": round(total_pnl, 2),
            "pnl_pct": round(total_pnl_pct, 4),
            "r_multiple": round(r_multiple, 2),
            "fees": round(total_fees, 2),
            "exit_reason": reason,
            "market_regime": position.market_regime,
            "equity_after": round(self.equity, 2),
        }

        self.closed_trades.append(result)
        return result

    def get_status(self) -> dict:
        return {
            "equity": round(self.equity, 2),
            "initial_equity": self.initial_equity,
            "total_pnl": round(self.equity - self.initial_equity, 2),
            "total_pnl_pct": round((self.equity - self.initial_equity) / self.initial_equity * 100, 4),
            "open_positions": len(self.positions),
            "closed_trades": len(self.closed_trades),
            "is_paused": self.is_paused,
            "risk": self.risk_engine.get_status(),
        }

    def pause(self):
        self.is_paused = True
        logger.info("Paper trading paused")

    def resume(self):
        self.is_paused = False
        logger.info("Paper trading resumed")
