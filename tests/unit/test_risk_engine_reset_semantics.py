"""Phase 2.6: the risk engine must distinguish three separate mechanisms
with three separate reset semantics --
  - Daily loss limit:      auto-resets on the next UTC calendar day.
  - Consecutive-loss cooldown: auto-expires after cooldown_minutes.
  - Max-drawdown hard kill: NEVER auto-resets; only reset_hard_kill() clears it.

This is the fix for the Phase 2.5 finding: both daily-loss and drawdown
used to set the same permanent `kill_switch` flag, so a single bad day
silently locked a multi-year backtest out of trading forever after.
"""
from datetime import datetime, timedelta

import pytest

from services.risk_engine.engine import RiskEngine, RiskConfig, RiskStatus


def _engine(**overrides) -> RiskEngine:
    defaults = dict(
        risk_per_trade_pct=0.5,
        max_daily_loss_pct=2.0,
        max_drawdown_pct=10.0,
        max_leverage=5,
        max_positions=1,
        max_daily_trades=10,
        cooldown_consecutive_losses=3,
        cooldown_minutes=60,
    )
    defaults.update(overrides)
    return RiskEngine(RiskConfig(**defaults), initial_equity=10000)


# ---------------------------------------------------------------------------
# Daily reset tests (section 13)
# ---------------------------------------------------------------------------

def test_daily_loss_limit_blocks_trades_same_day():
    re = _engine()
    day1 = datetime(2024, 1, 1, 10)
    re.record_trade_result(-2.5, now=day1)  # breaches -2.0% daily loss

    can, reason = re.can_trade(now=day1 + timedelta(hours=1))
    assert can is False
    assert reason.startswith(RiskStatus.DAILY_LIMIT.value)
    assert re.get_risk_status(day1 + timedelta(hours=1)) == RiskStatus.DAILY_LIMIT


def test_daily_loss_limit_remains_blocked_later_same_day():
    re = _engine()
    day1 = datetime(2024, 1, 1, 1)
    re.record_trade_result(-2.5, now=day1)
    can, _ = re.can_trade(now=datetime(2024, 1, 1, 23, 59))
    assert can is False


def test_daily_loss_limit_resets_at_utc_midnight():
    re = _engine()
    day1 = datetime(2024, 1, 1, 10)
    re.record_trade_result(-2.5, now=day1)
    assert re.can_trade(now=day1)[0] is False

    day2 = datetime(2024, 1, 2, 0, 0, 1)  # just after UTC midnight
    can, reason = re.can_trade(now=day2)
    assert can is True, f"expected daily loss limit to reset at UTC midnight, got: {reason}"
    assert re.get_risk_status(day2) == RiskStatus.ACTIVE


def test_daily_loss_limit_does_not_set_hard_kill():
    """The core Phase 2.5 bug: a same-day loss limit must NOT permanently
    disable trading -- only max-drawdown should do that."""
    re = _engine(max_drawdown_pct=50.0)  # keep drawdown far out of reach
    re.record_trade_result(-2.5, now=datetime(2024, 1, 1, 10))
    assert re.state.kill_switch is False


def test_daily_trade_counter_resets_with_daily_loss():
    re = _engine(max_daily_trades=2)
    day1 = datetime(2024, 1, 1, 10)
    re.record_trade_result(0.1, now=day1)
    re.record_trade_result(0.1, now=day1)
    assert re.can_trade(now=day1)[0] is False  # max_daily_trades reached

    day2 = datetime(2024, 1, 2, 10)
    can, _ = re.can_trade(now=day2)
    assert can is True


# ---------------------------------------------------------------------------
# Consecutive-loss cooldown tests (section 13)
# ---------------------------------------------------------------------------

def test_one_and_two_losses_do_not_trigger_cooldown():
    re = _engine()
    t0 = datetime(2024, 1, 1, 0)
    re.record_trade_result(-0.1, now=t0)
    assert re.can_trade(now=t0)[0] is True
    re.record_trade_result(-0.1, now=t0 + timedelta(minutes=1))
    assert re.can_trade(now=t0 + timedelta(minutes=1))[0] is True


def test_third_consecutive_loss_starts_cooldown():
    re = _engine()
    t0 = datetime(2024, 1, 1, 0)
    for i in range(3):
        re.record_trade_result(-0.1, now=t0 + timedelta(minutes=i))

    can, reason = re.can_trade(now=t0 + timedelta(minutes=3))
    assert can is False
    assert reason.startswith(RiskStatus.COOLDOWN.value)
    assert re.state.cooldown_until == t0 + timedelta(minutes=2) + timedelta(minutes=60)


def test_cooldown_boundary_exact_second():
    re = _engine(cooldown_minutes=60)
    t0 = datetime(2024, 1, 1, 0)
    last_loss_time = t0
    for i in range(3):
        re.record_trade_result(-0.1, now=last_loss_time)
        last_loss_time = last_loss_time + timedelta(minutes=1) if i < 2 else last_loss_time
    cooldown_until = re.state.cooldown_until

    just_before = cooldown_until - timedelta(seconds=1)
    assert re.can_trade(now=just_before)[0] is False, "must still be blocked 1 second before cooldown expiry"

    exactly_at = cooldown_until
    assert re.can_trade(now=exactly_at)[0] is True, "must be allowed exactly at cooldown expiry"


def test_trading_resumes_after_cooldown_expires():
    re = _engine(cooldown_minutes=60)
    t0 = datetime(2024, 1, 1, 0)
    for i in range(3):
        re.record_trade_result(-0.1, now=t0 + timedelta(minutes=i))

    assert re.can_trade(now=t0 + timedelta(minutes=10))[0] is False
    assert re.can_trade(now=t0 + timedelta(minutes=65))[0] is True


def test_win_resets_consecutive_loss_counter():
    re = _engine()
    t0 = datetime(2024, 1, 1, 0)
    re.record_trade_result(-0.1, now=t0)
    re.record_trade_result(-0.1, now=t0 + timedelta(minutes=1))
    re.record_trade_result(0.5, now=t0 + timedelta(minutes=2))  # WIN
    assert re.state.consecutive_losses == 0
    assert re.state.cooldown_until is None

    # a 3rd loss now should NOT immediately trigger cooldown (streak was reset)
    re.record_trade_result(-0.1, now=t0 + timedelta(minutes=3))
    assert re.can_trade(now=t0 + timedelta(minutes=3))[0] is True


def test_breakeven_resets_consecutive_losses_by_default_policy():
    re = _engine()
    t0 = datetime(2024, 1, 1, 0)
    re.record_trade_result(-0.1, now=t0)
    re.record_trade_result(-0.1, now=t0 + timedelta(minutes=1))
    re.record_trade_result(0.0, now=t0 + timedelta(minutes=2))  # BREAKEVEN
    assert re.state.consecutive_losses == 0, "breakeven should reset the streak under the default policy"


def test_breakeven_policy_is_configurable_to_not_reset():
    re = _engine(breakeven_resets_consecutive_losses=False)
    t0 = datetime(2024, 1, 1, 0)
    re.record_trade_result(-0.1, now=t0)
    re.record_trade_result(-0.1, now=t0 + timedelta(minutes=1))
    re.record_trade_result(0.0, now=t0 + timedelta(minutes=2))  # BREAKEVEN, policy disabled
    assert re.state.consecutive_losses == 2, "breakeven must not reset the streak when the policy is disabled"


def test_cooldown_minutes_and_threshold_are_configurable_not_hardcoded():
    re = _engine(cooldown_consecutive_losses=2, cooldown_minutes=15)
    t0 = datetime(2024, 1, 1, 0)
    re.record_trade_result(-0.1, now=t0)
    re.record_trade_result(-0.1, now=t0 + timedelta(minutes=1))  # 2nd loss triggers cooldown now
    assert re.can_trade(now=t0 + timedelta(minutes=1))[0] is False
    assert re.state.cooldown_until == t0 + timedelta(minutes=1) + timedelta(minutes=15)
    assert re.can_trade(now=t0 + timedelta(minutes=16, seconds=1))[0] is True


# ---------------------------------------------------------------------------
# Hard-kill tests (section 13)
# ---------------------------------------------------------------------------

def test_max_drawdown_breach_activates_hard_kill():
    re = _engine(max_drawdown_pct=10.0)
    re.record_trade_result(-15.0, now=datetime(2024, 1, 1, 10))
    assert re.state.kill_switch is True
    assert re.get_risk_status(datetime(2024, 1, 1, 10)) == RiskStatus.HARD_KILL


def test_hard_kill_survives_utc_day_change():
    re = _engine(max_drawdown_pct=10.0)
    re.record_trade_result(-15.0, now=datetime(2024, 1, 1, 10))
    assert re.can_trade(now=datetime(2024, 1, 2, 10))[0] is False, "hard kill must NOT clear on daily reset"
    assert re.state.kill_switch is True


def test_hard_kill_survives_cooldown_expiry():
    re = _engine(max_drawdown_pct=10.0, cooldown_consecutive_losses=3, cooldown_minutes=1)
    t0 = datetime(2024, 1, 1, 0)
    # trigger 2 small losses (no drawdown breach yet) then a catastrophic one
    re.record_trade_result(-0.1, now=t0)
    re.record_trade_result(-0.1, now=t0 + timedelta(minutes=1))
    re.record_trade_result(-15.0, now=t0 + timedelta(minutes=2))  # 3rd loss AND drawdown breach

    much_later = t0 + timedelta(days=30)  # cooldown (1 min) long since expired
    can, reason = re.can_trade(now=much_later)
    assert can is False, "hard kill must remain active even though the cooldown window has long expired"
    assert reason.startswith(RiskStatus.HARD_KILL.value)


def test_explicit_manual_reset_clears_hard_kill():
    # max_daily_loss_pct set generously high so this test isolates the hard
    # kill mechanism specifically, not an overlapping same-day loss-limit block.
    re = _engine(max_drawdown_pct=10.0, max_daily_loss_pct=90.0)
    re.record_trade_result(-15.0, now=datetime(2024, 1, 1, 10))
    assert re.state.kill_switch is True

    re.reset_hard_kill()
    assert re.state.kill_switch is False
    assert re.can_trade(now=datetime(2024, 1, 1, 11))[0] is True


def test_deactivate_kill_switch_is_a_backward_compatible_alias():
    re = _engine(max_drawdown_pct=10.0)
    re.record_trade_result(-15.0, now=datetime(2024, 1, 1, 10))
    re.deactivate_kill_switch()
    assert re.state.kill_switch is False


def test_hard_kill_full_sequence_day_change_then_cooldown_expiry_then_manual_reset():
    """Section 13's exact scenario: drawdown breach -> UTC day change ->
    daily reset occurs -> hard kill remains -> cooldown expires -> hard
    kill still remains -> explicit manual reset clears it."""
    re = _engine(max_drawdown_pct=10.0, cooldown_consecutive_losses=3, cooldown_minutes=5)
    t0 = datetime(2024, 1, 1, 0)
    re.record_trade_result(-0.1, now=t0)
    re.record_trade_result(-0.1, now=t0 + timedelta(minutes=1))
    re.record_trade_result(-15.0, now=t0 + timedelta(minutes=2))
    assert re.state.kill_switch is True

    # UTC day change -> daily reset occurs, hard kill remains
    day2 = datetime(2024, 1, 2, 0, 5)
    re.reset_daily(day2)
    assert re.state.daily_pnl_pct == 0  # daily state DID reset
    assert re.state.kill_switch is True  # hard kill did NOT

    # cooldown (5 min) long expired -> still remains
    assert re.can_trade(now=day2)[0] is False
    assert re.get_risk_status(day2) == RiskStatus.HARD_KILL

    # explicit manual reset -> clears it
    re.reset_hard_kill()
    assert re.can_trade(now=day2)[0] is True


# ---------------------------------------------------------------------------
# Simulated-clock tests (section 13)
# ---------------------------------------------------------------------------

def test_risk_decisions_use_only_the_provided_simulated_timestamp():
    """The machine's real date must have zero influence on risk decisions
    when a simulated `now` is supplied -- this is the whole point of the
    Phase 2.5/2.6 fix."""
    re = _engine()
    historical_now = datetime(2023, 8, 27, 12, 0, 0)  # far from the real machine date
    re.record_trade_result(-2.5, now=historical_now)

    can, reason = re.can_trade(now=historical_now)
    assert can is False
    assert reason.startswith(RiskStatus.DAILY_LIMIT.value)

    next_historical_day = datetime(2023, 8, 28, 0, 0, 1)
    can2, _ = re.can_trade(now=next_historical_day)
    assert can2 is True, "daily reset must be driven by the simulated date, not the real machine date"


def test_get_risk_status_is_a_pure_function_of_now():
    re = _engine(cooldown_consecutive_losses=3, cooldown_minutes=60)
    t0 = datetime(2024, 6, 1, 0)
    for i in range(3):
        re.record_trade_result(-0.1, now=t0 + timedelta(minutes=i))
    assert re.get_risk_status(t0 + timedelta(minutes=10)) == RiskStatus.COOLDOWN
    assert re.get_risk_status(t0 + timedelta(minutes=65)) == RiskStatus.ACTIVE
