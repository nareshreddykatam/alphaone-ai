import enum
from dataclasses import dataclass
from datetime import datetime, date, timedelta
import structlog

logger = structlog.get_logger()


class RiskStatus(str, enum.Enum):
    """Observable, named risk states -- see docs/risk_management.md.

    Three DISTINCT mechanisms, each with different reset semantics:
    - DAILY_LIMIT:  automatic reset on the next UTC calendar day.
    - COOLDOWN:     automatic expiry after `cooldown_minutes` of (simulated
                    or real) elapsed time.
    - HARD_KILL:    NEVER auto-resets. Requires an explicit, auditable call
                    to `RiskEngine.reset_hard_kill()`.
    """
    ACTIVE = "ACTIVE"
    DAILY_LIMIT = "DAILY_LIMIT"
    COOLDOWN = "COOLDOWN"
    HARD_KILL = "HARD_KILL"


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.5
    max_daily_loss_pct: float = 2.0
    max_drawdown_pct: float = 10.0
    max_leverage: int = 5
    max_positions: int = 1
    max_daily_trades: int = 10
    cooldown_consecutive_losses: int = 3
    cooldown_minutes: int = 60
    # Explicit, configurable policy (Phase 2.6, section 8): a trade that
    # closes at exactly breakeven (pnl_pct == 0) is treated as a non-loss
    # and resets the consecutive-loss counter, same as a win. Set to False
    # to instead treat breakeven as "neither win nor loss" (counter held
    # unchanged) if a future strategy needs that distinction.
    breakeven_resets_consecutive_losses: bool = True


@dataclass
class RiskState:
    daily_pnl_pct: float = 0
    daily_trades: int = 0
    current_drawdown_pct: float = 0
    peak_equity: float = 10000
    current_equity: float = 10000
    consecutive_losses: int = 0
    last_loss_time: datetime | None = None
    # Explicit cooldown expiry timestamp (Phase 2.6, section 9), set the
    # moment the Nth consecutive loss occurs. Replaces recomputing "elapsed
    # time since last loss" on every can_trade() call with a single
    # precomputed boundary: `now < cooldown_until` blocks, `now >=
    # cooldown_until` allows -- exact, testable, and clock-source agnostic.
    cooldown_until: datetime | None = None
    # HARD kill only -- max-drawdown breach. Never set by the daily-loss
    # path (that was the Phase 2.5 bug: both mechanisms shared this one
    # flag, so a same-day loss limit permanently ended the backtest).
    # Manual reset only, via `RiskEngine.reset_hard_kill()`.
    kill_switch: bool = False
    positions_open: int = 0


class RiskEngine:
    def __init__(self, config: RiskConfig | None = None, initial_equity: float = 10000):
        self.config = config or RiskConfig()
        self.state = RiskState(peak_equity=initial_equity, current_equity=initial_equity)
        self.initial_equity = initial_equity
        self._trade_date = date.today()

    def reset_daily(self, now: datetime | None = None):
        """Reset the daily counters when the (simulated or real) UTC
        calendar day changes.

        `now` must be the caller's notion of "current time" -- real
        wall-clock UTC time for paper/live trading, but the *simulated*
        candle timestamp during a backtest. Reading the real system clock
        here unconditionally was a critical Phase 2 bug: a backtest loop
        iterating months of historical timestamps finishes in seconds of
        real time, so `date.today()` never advances and daily limits
        silently became whole-backtest limits.

        Only `daily_pnl_pct` and `daily_trades` reset here. Per Phase 2.6
        section 7, this must NEVER reset: max-drawdown history, the hard
        kill switch, or consecutive-loss/cooldown state.
        """
        now = now or datetime.utcnow()
        today = now.date()
        if self._trade_date != today:
            self.state.daily_pnl_pct = 0
            self.state.daily_trades = 0
            self._trade_date = today

    def get_risk_status(self, now: datetime | None = None) -> RiskStatus:
        """Classify the current blocking reason, if any, among the three
        named risk mechanisms (capacity gates like max_positions/
        max_daily_trades are separate and not part of this state model --
        see `can_trade` for those). Pure inspection, no side effects other
        than the day-rollover check every read already needs."""
        now = now or datetime.utcnow()
        self.reset_daily(now)

        if self.state.kill_switch:
            return RiskStatus.HARD_KILL
        if self.state.daily_pnl_pct <= -self.config.max_daily_loss_pct:
            return RiskStatus.DAILY_LIMIT
        if (
            self.state.consecutive_losses >= self.config.cooldown_consecutive_losses
            and self.state.cooldown_until is not None
            and now < self.state.cooldown_until
        ):
            return RiskStatus.COOLDOWN
        return RiskStatus.ACTIVE

    def can_trade(self, now: datetime | None = None) -> tuple[bool, str]:
        now = now or datetime.utcnow()
        self.reset_daily(now)

        if self.state.kill_switch:
            return False, (
                f"{RiskStatus.HARD_KILL.value}: Kill switch is active (max drawdown breached) "
                f"-- manual reset required via reset_hard_kill()"
            )

        if self.state.daily_pnl_pct <= -self.config.max_daily_loss_pct:
            return False, (
                f"{RiskStatus.DAILY_LIMIT.value}: Max daily loss exceeded: "
                f"{self.state.daily_pnl_pct:.2f}% (resets automatically at the next UTC day)"
            )

        if self.state.daily_trades >= self.config.max_daily_trades:
            return False, f"Max daily trades reached: {self.state.daily_trades}"

        if self.state.positions_open >= self.config.max_positions:
            return False, f"Max positions reached: {self.state.positions_open}"

        if (
            self.state.consecutive_losses >= self.config.cooldown_consecutive_losses
            and self.state.cooldown_until is not None
            and now < self.state.cooldown_until
        ):
            return False, (
                f"{RiskStatus.COOLDOWN.value}: Cooldown after {self.state.consecutive_losses} "
                f"consecutive losses, resumes at {self.state.cooldown_until.isoformat()}"
            )

        # NOTE: deliberately NOT re-deriving a block from
        # `current_drawdown_pct` here. `kill_switch` (checked above) is the
        # single, authoritative source of truth for the hard-kill state --
        # re-checking the raw drawdown percentage would make
        # `reset_hard_kill()` a no-op whenever equity hasn't yet recovered
        # above the threshold, which defeats the entire point of an
        # explicit manual override (a human choosing to resume despite
        # still being in a drawdown, at their own informed discretion).
        # record_trade_result() is the single place that arms kill_switch
        # from a drawdown breach.

        return True, "OK"

    def calculate_position_size(
        self, entry_price: float, stop_loss: float, leverage: int = 1
    ) -> float:
        risk_amount = self.state.current_equity * (self.config.risk_per_trade_pct / 100)
        price_risk = abs(entry_price - stop_loss)

        if price_risk == 0:
            return 0

        leverage = min(leverage, self.config.max_leverage)
        position_size = (risk_amount * leverage) / price_risk

        max_notional = self.state.current_equity * leverage
        max_quantity = max_notional / entry_price

        return min(position_size, max_quantity)

    def validate_trade(
        self, entry_price: float, stop_loss: float, take_profit: float, side: str
    ) -> tuple[bool, str, float]:
        can, reason = self.can_trade()
        if not can:
            return False, reason, 0

        if entry_price <= 0 or stop_loss <= 0 or take_profit <= 0:
            return False, "Invalid price levels", 0

        risk = abs(entry_price - stop_loss)
        reward = abs(take_profit - entry_price)

        if risk == 0:
            return False, "Zero risk - invalid stop loss", 0

        rr = reward / risk
        if rr < 1.0:
            return False, f"Risk/reward too low: 1:{rr:.2f}", 0

        quantity = self.calculate_position_size(entry_price, stop_loss)
        if quantity <= 0:
            return False, "Position size is zero", 0

        return True, "OK", quantity

    def record_trade_result(self, pnl_pct: float, now: datetime | None = None):
        now = now or datetime.utcnow()
        # Keep `_trade_date` in sync with the caller's clock even if
        # `record_trade_result` is ever called before `can_trade`/`reset_daily`
        # for this "day" (e.g. direct RiskEngine use outside the backtester
        # loop) -- otherwise the next reset_daily() call could see a stale
        # `_trade_date` and wrongly wipe counters that belong to the current day.
        self.reset_daily(now)
        self.state.daily_pnl_pct += pnl_pct
        self.state.current_equity *= (1 + pnl_pct / 100)
        self.state.daily_trades += 1

        if self.state.current_equity > self.state.peak_equity:
            self.state.peak_equity = self.state.current_equity

        if self.state.peak_equity > 0:
            self.state.current_drawdown_pct = (
                (self.state.peak_equity - self.state.current_equity) / self.state.peak_equity * 100
            )

        # Consecutive-loss / cooldown semantics (Phase 2.6, sections 8-9):
        # a LOSS (pnl_pct < 0) increments the streak. A WIN, or a BREAKEVEN
        # trade when `breakeven_resets_consecutive_losses` is enabled
        # (the default), resets it to zero and clears any pending cooldown.
        is_loss = pnl_pct < 0
        is_breakeven = pnl_pct == 0
        resets_streak = (not is_loss) and (not is_breakeven or self.config.breakeven_resets_consecutive_losses)

        if is_loss:
            self.state.consecutive_losses += 1
            self.state.last_loss_time = now
            if self.state.consecutive_losses >= self.config.cooldown_consecutive_losses:
                self.state.cooldown_until = now + timedelta(minutes=self.config.cooldown_minutes)
                logger.warning(
                    "Cooldown started after consecutive losses",
                    consecutive_losses=self.state.consecutive_losses,
                    cooldown_until=self.state.cooldown_until,
                )
        elif resets_streak:
            self.state.consecutive_losses = 0
            self.state.cooldown_until = None

        # HARD KILL -- max drawdown only. Deliberately never auto-resets;
        # see reset_hard_kill(). This is the ONLY place that sets it.
        if self.state.current_drawdown_pct >= self.config.max_drawdown_pct:
            if not self.state.kill_switch:
                logger.critical(
                    "HARD KILL: max drawdown exceeded -- manual reset required",
                    drawdown=self.state.current_drawdown_pct,
                )
            self.state.kill_switch = True

        # Daily loss limit -- deliberately does NOT touch kill_switch. It is
        # enforced live in can_trade() via daily_pnl_pct, which reset_daily()
        # already clears on the next UTC day. Sharing the hard-kill flag
        # with this condition was the Phase 2.5 bug (a single bad day
        # permanently ended a multi-year backtest).
        if self.state.daily_pnl_pct <= -self.config.max_daily_loss_pct:
            logger.warning(
                "Daily loss limit reached -- blocking further trades until the next UTC day",
                daily_loss=self.state.daily_pnl_pct,
            )

    def activate_kill_switch(self):
        """Manually arm the hard kill (e.g. an operator's emergency stop)."""
        self.state.kill_switch = True
        logger.warning("Kill switch manually activated")

    def reset_hard_kill(self):
        """The ONLY way to clear a hard kill (max-drawdown breach or manual
        activation). Explicit and auditable by design -- daily reset and
        cooldown expiry must never call this. Also clears the consecutive-
        loss streak/cooldown, treating this as a deliberate fresh start."""
        self.state.kill_switch = False
        self.state.consecutive_losses = 0
        self.state.cooldown_until = None
        logger.info("Hard kill switch manually reset")

    def deactivate_kill_switch(self):
        """Backward-compatible alias for reset_hard_kill()."""
        self.reset_hard_kill()

    def record_position_open(self):
        self.state.positions_open += 1

    def record_position_close(self):
        self.state.positions_open = max(0, self.state.positions_open - 1)

    def get_status(self) -> dict:
        now = datetime.utcnow()
        self.reset_daily(now)
        return {
            "risk_per_trade_pct": self.config.risk_per_trade_pct,
            "max_daily_loss_pct": self.config.max_daily_loss_pct,
            "max_drawdown_pct": self.config.max_drawdown_pct,
            "current_daily_pnl_pct": round(self.state.daily_pnl_pct, 4),
            "current_drawdown_pct": round(self.state.current_drawdown_pct, 4),
            "positions_open": self.state.positions_open,
            "max_positions": self.config.max_positions,
            "trades_today": self.state.daily_trades,
            "max_daily_trades": self.config.max_daily_trades,
            "kill_switch_active": self.state.kill_switch,
            "current_equity": round(self.state.current_equity, 2),
            "peak_equity": round(self.state.peak_equity, 2),
            "consecutive_losses": self.state.consecutive_losses,
            "cooldown_until": self.state.cooldown_until.isoformat() if self.state.cooldown_until else None,
            "risk_status": self.get_risk_status(now).value,
        }
