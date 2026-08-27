from dataclasses import dataclass
from datetime import datetime
from typing import Optional
import structlog

from services.risk_engine.engine import RiskEngine, RiskConfig
from services.trade_journal.pnl import compute_slice_pnl, compute_r_multiple

logger = structlog.get_logger()


@dataclass
class PaperPosition:
    trade_id: str
    signal_id: str
    side: str
    entry_price: float
    entry_time: datetime
    stop_loss: float
    take_profit_1: float
    quantity: float
    leverage: int = 1
    market_regime: str = ""
    pnl: float = 0
    pnl_pct: float = 0


class PaperTrader:
    def __init__(self, risk_config: RiskConfig | None = None, initial_equity: float = 10000):
        self.risk_engine = RiskEngine(risk_config, initial_equity)
        self.initial_equity = initial_equity
        self.equity = initial_equity
        self.positions: dict[str, PaperPosition] = {}
        self.closed_trades: list[dict] = []
        self.trade_counter = 0
        self.is_paused = False

    def process_candle(self, candle) -> list[dict]:
        if self.is_paused:
            return []

        events = []
        for trade_id, pos in list(self.positions.items()):
            if pos.side == "LONG":
                if candle.low <= pos.stop_loss:
                    events.append(self._close_position(pos, pos.stop_loss, candle.timestamp, "stop_loss"))
                elif candle.high >= pos.take_profit_1:
                    events.append(self._close_position(pos, pos.take_profit_1, candle.timestamp, "take_profit"))
            else:
                if candle.high >= pos.stop_loss:
                    events.append(self._close_position(pos, pos.stop_loss, candle.timestamp, "stop_loss"))
                elif candle.low <= pos.take_profit_1:
                    events.append(self._close_position(pos, pos.take_profit_1, candle.timestamp, "take_profit"))

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
            quantity=quantity,
            leverage=leverage,
            market_regime=signal.market_regime,
        )

        self.positions[trade_id] = position
        self.risk_engine.record_position_open()

        logger.info("Paper position opened",
                    trade_id=trade_id, side=signal.signal_type,
                    entry=signal.entry_price, sl=signal.stop_loss,
                    tp=signal.take_profit_1, quantity=quantity)

        return position

    def _close_position(
        self, position: PaperPosition, exit_price: float, timestamp: datetime, reason: str
    ) -> dict:
        slice_pnl = compute_slice_pnl(
            position.side, position.entry_price, exit_price, position.quantity, position.leverage
        )
        pnl, fee, pnl_pct = slice_pnl.pnl, slice_pnl.fees, slice_pnl.pnl_pct
        r_multiple = compute_r_multiple(position.entry_price, position.stop_loss, position.quantity, pnl)

        self.equity += pnl
        self.risk_engine.record_trade_result(pnl_pct)
        self.risk_engine.record_position_close()

        del self.positions[position.trade_id]

        result = {
            "trade_id": position.trade_id,
            "signal_id": position.signal_id,
            "side": position.side,
            "entry_price": position.entry_price,
            "exit_price": exit_price,
            "entry_time": position.entry_time,
            "exit_time": timestamp,
            "stop_loss": position.stop_loss,
            "take_profit": position.take_profit_1,
            "quantity": position.quantity,
            "leverage": position.leverage,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 4),
            "r_multiple": round(r_multiple, 2),
            "fees": round(fee, 2),
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
