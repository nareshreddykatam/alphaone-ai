"""Telegram bot (Phase 4E, updated Phase 5 for CoinDCX). Read-only
reporting + manual-tracking helpers only -- no command here can place,
cancel, or modify a trade; AlphaOne is architecturally incapable of that
(see services/exchange/coindcx.py and
tests/unit/test_no_order_placement_capability.py). Every handler queries
the real DB/CoinDCX through the same services the API/dashboard use, so
Telegram and the website can never show contradictory numbers.

No live Telegram testing was done in this session (per the user's explicit
choice to build against mocks only, see tests/unit/test_telegram_bot.py) --
supply a real BOT_TOKEN via .env and call `build_app().run_polling()` (see
`if __name__ == "__main__"` below) to go live; nothing else needs to change.
"""
from datetime import datetime

from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from sqlalchemy import select
import structlog

from apps.api.config import get_settings
from database.schema import async_session
from database.schema.models import Signal
from services.common.currency import format_inr, format_usdt
from services.exchange.coindcx import CoinDCXReadOnlyAccountProvider
from services.exchange.fx import get_usdt_inr_rate, convert_usdt_to_inr
from services.portfolio.account import get_or_create_default_account
from services.portfolio.service import get_user_actual_performance, get_pnl_breakdown, period_key
from services.risk_engine.state_store import load_risk_engine
from services.signal_engine.live_signal import is_signal_generation_paused, set_signal_generation_paused
from services.trade_journal.journal import get_open_trades

logger = structlog.get_logger()
settings = get_settings()


class TelegramBot:
    def __init__(self, bot_token: str = "", chat_id: str = ""):
        self.bot_token = bot_token or settings.telegram_bot_token
        self.chat_id = chat_id or settings.telegram_chat_id
        self.enabled = settings.telegram_enabled
        self._app = None

    def build_app(self) -> Application:
        self._app = Application.builder().token(self.bot_token).build()
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("account", self._cmd_account))
        self._app.add_handler(CommandHandler("signal", self._cmd_signal))
        self._app.add_handler(CommandHandler("position", self._cmd_position))
        self._app.add_handler(CommandHandler("trades", self._cmd_trades))
        self._app.add_handler(CommandHandler("performance", self._cmd_performance))
        self._app.add_handler(CommandHandler("today", self._cmd_today))
        self._app.add_handler(CommandHandler("risk", self._cmd_risk))
        self._app.add_handler(CommandHandler("pause", self._cmd_pause))
        self._app.add_handler(CommandHandler("resume", self._cmd_resume))
        return self._app

    # ---- outbound alerts (unchanged formatting, still functional) --------

    async def send_signal(self, signal: dict):
        if not self.enabled:
            return
        signal_type = signal.get("signal_type", "NO_TRADE")
        if signal_type == "NO_TRADE":
            return

        # BTC/USDT Perpetual is the actual CoinDCX trading instrument --
        # USDT is the PRIMARY/authoritative trading denomination for every
        # level below; INR is only the SECONDARY converted representation
        # (never invented when the conversion rate is unavailable -- the
        # USDT levels, which are what the user actually executes on
        # CoinDCX, are always shown regardless of conversion availability).
        rate = await get_usdt_inr_rate()

        def level(usdt_value):
            usdt_line = format_usdt(usdt_value)
            inr_line = format_inr(convert_usdt_to_inr(usdt_value, rate)) if rate is not None else "INR conversion unavailable"
            return f"{usdt_line}\n≈ {inr_line}" if rate is not None else f"{usdt_line}\n({inr_line})"

        emoji = "🟢" if signal_type == "LONG" else "🔴"
        tp_lines = [f"TP1:\n{level(signal.get('take_profit_1'))}"]
        if signal.get("take_profit_2") is not None:
            tp_lines.append(f"TP2:\n{level(signal.get('take_profit_2'))}")
        if signal.get("take_profit_3") is not None:
            tp_lines.append(f"TP3:\n{level(signal.get('take_profit_3'))}")

        # Every independently-firing strategy (services/signal_engine/
        # multi_strategy.py) gets its OWN message, clearly labeled with
        # which strategy + timeframe fired -- never merged with another
        # strategy's signal, never implying consensus.
        strategy_id = signal.get("strategy_id") or signal.get("strategy_name", "trend_following_donchian_adx")
        strategy_display = signal.get("strategy_display_name", strategy_id)
        timeframe = signal.get("timeframe") or "4h"

        text = (
            f"🚨 BTC/USDT FUTURES\n\n"
            f"Strategy: {strategy_id} — {strategy_display}\n"
            f"Timeframe: {timeframe}\n\n"
            f"{emoji} {signal_type}\n\n"
            f"Market:\nCoinDCX BTC/USDT Perpetual\n\n"
            f"Quality: {signal.get('quality', 'LOW')} (categorical, not an accuracy guarantee)\n\n"
            f"Entry:\n{level(signal.get('entry_price'))}\n\n"
            f"Stop Loss:\n{level(signal.get('stop_loss'))}\n\n"
            + "\n\n".join(tp_lines) + "\n\n"
            f"Risk/Reward:\n1 : {signal.get('risk_reward', 0)}\n\n"
            f"Market Regime:\n{signal.get('market_regime', 'UNKNOWN')}\n\n"
            f"Reason:\n{signal.get('reasoning', '')}\n\n"
            f"Price source:\nCoinDCX Live Market Data\n\n"
            f"Signal ID:\n{signal.get('signal_id', 'unknown')}\n\n"
            f"MANUAL EXECUTION REQUIRED -- you execute this manually on CoinDCX, AlphaOne does not place orders."
        )
        await self._send(text)

    async def send_position_detected(self, position: dict):
        """Sent when a new CoinDCX position is detected during sync
        (Phase 5 section 27) -- purely informational, AlphaOne did not
        cause this position to exist."""
        if not self.enabled:
            return
        text = (
            f"POSITION DETECTED\n\n"
            f"{position.get('symbol')}\n\n"
            f"{position.get('side')}\n\n"
            f"Entry:\n{format_inr(position.get('entry_price'))}\n\n"
            f"Quantity:\n{position.get('quantity')}\n\n"
            f"Leverage:\n{position.get('leverage')}\n\n"
            f"Liquidation:\n{format_inr(position.get('liquidation_price'))}\n\n"
            f"AlphaOne Signal:\n{position.get('signal_id') or 'UNMATCHED'}\n\n"
            f"Tracking started."
        )
        await self._send(text)

    async def send_exit_alert(self, alert: dict):
        """A RECOMMENDATION only (Phase 5 section 28) -- the position
        monitor found that an open position's own stop-loss/take-profit,
        or another exit condition, was reached. AlphaOne has not closed
        anything; the user must execute the exit on CoinDCX themselves and
        then log/sync it."""
        if not self.enabled:
            return
        reason_label = {
            "stop_loss_hit": "Stop loss",
            "take_profit_hit": "Take profit",
        }.get(alert.get("reason"), alert.get("reason", "Exit condition"))
        pnl = alert.get("pnl", 0) or 0
        text = (
            f"EXIT ALERT\n\n"
            f"{alert.get('symbol')}\n\n"
            f"{alert.get('side')}\n\n"
            f"Entry:\n{format_inr(alert.get('entry_price'))}\n\n"
            f"Current:\n{format_inr(alert.get('current_price'))}\n\n"
            f"P&L:\n{format_inr(pnl, show_sign=True)}\n\n"
            f"Reason:\n{reason_label}\n\n"
            f"ACTION:\nCONSIDER MANUAL EXIT ON COINDCX\n\n"
            f"MANUAL EXECUTION REQUIRED"
        )
        await self._send(text)

    async def send_exit(self, trade: dict):
        if not self.enabled:
            return
        pnl = trade.get("pnl", 0)
        emoji = "✅" if pnl >= 0 else "❌"
        text = (
            f"📤 TRADE CLOSED\n\n"
            f"{emoji} {trade.get('side', '')}\n\n"
            f"Entry: {format_inr(trade.get('entry_price'))}\n"
            f"Exit: {format_inr(trade.get('exit_price'))}\n"
            f"P&L: {format_inr(pnl, show_sign=True)} ({trade.get('pnl_pct', 0):.2f}%)\n"
            f"R Multiple: {trade.get('r_multiple', 0):.2f}R\n"
            f"Reason: {trade.get('exit_reason', '')}"
        )
        await self._send(text)

    async def send_market_data_alert(self, status: str):
        """A connection state-transition alert (Phase: Live Market Data) --
        fired at most once per real transition by
        services/market_data/coindcx_ws.py's own state machine, never on
        every poll/tick. Purely informational: this is about the public
        BTC/USDT price feed, not the user's account or any position."""
        if not self.enabled:
            return
        if status == "DISCONNECTED":
            text = (
                "⚠️ COINDCX MARKET DATA DISCONNECTED\n\n"
                "The live BTC/USDT market-data stream has disconnected. "
                "Dashboard price and freshness may lag until it recovers.\n\n"
                "This does not affect your CoinDCX account or any open position."
            )
        else:
            text = (
                "✅ COINDCX MARKET DATA RECOVERED\n\n"
                "The live BTC/USDT market-data stream has reconnected."
            )
        await self._send(text)

    async def send_kill_switch(self, reason: str):
        if not self.enabled:
            return
        text = (
            f"🚨 RISK ENGINE: HARD KILL\n\n"
            f"Reason: {reason}\n"
            f"This is informational only -- AlphaOne cannot place trades. "
            f"Consider pausing your own manual trading until you've reviewed this."
        )
        await self._send(text)

    async def _send(self, text: str):
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram not configured")
            return
        try:
            bot = Bot(token=self.bot_token)
            await bot.send_message(chat_id=self.chat_id, text=text)
            logger.info("Telegram message sent")
        except Exception as e:
            logger.error("Telegram send failed", error=str(e))

    # ---- inbound commands, all real DB reads --------------------------

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🤖 AlphaOne\n\n"
            "Trading intelligence and manual trade tracking for BTC/USDT perpetuals on CoinDCX.\n"
            "AlphaOne never places, cancels, or modifies orders -- you execute everything manually.\n\n"
            "Use /help for available commands."
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Available commands:\n\n"
            "/status - Risk engine status\n"
            "/account - Account connection status\n"
            "/signal - Latest research signal\n"
            "/position - Your open manual trades\n"
            "/trades - Recent trade history\n"
            "/performance - Your actual trading performance\n"
            "/today - Today's realized P&L\n"
            "/risk - Full risk dashboard\n"
            "/pause - Pause on-demand signal generation\n"
            "/resume - Resume on-demand signal generation\n"
            "/help - This help message"
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        async with async_session() as session:
            engine = await load_risk_engine(session)
            paused = await is_signal_generation_paused(session)
        status = engine.get_risk_status()
        await update.message.reply_text(
            f"Risk status: {status.value}\n"
            f"Signal generation: {'PAUSED' if paused else 'active'}\n"
            f"Mode: manual trade tracking (AlphaOne never auto-trades)"
        )

    async def _cmd_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        provider = CoinDCXReadOnlyAccountProvider(settings.coindcx_api_key, settings.coindcx_api_secret)
        balance = await provider.get_balance()

        if balance["status"] != "OK":
            await update.message.reply_text(
                f"COINDCX ACCOUNT\n\nData: {balance['status']}\n"
                f"(no live account data available -- see /status)"
            )
            return

        await update.message.reply_text(
            f"COINDCX ACCOUNT\n\n"
            f"Equity: {format_inr(balance['total_equity'])}\n"
            f"Available: {format_inr(balance['available_balance'])}\n"
            f"Used Margin: {format_inr(balance['used_margin'])}\n\n"
            f"Data: LIVE"
        )

    async def _cmd_signal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        async with async_session() as session:
            result = await session.execute(select(Signal).order_by(Signal.timestamp.desc()).limit(1))
            signal = result.scalar_one_or_none()
        if signal is None:
            await update.message.reply_text("No signal has been generated yet.")
            return
        rate = await get_usdt_inr_rate()

        def level(usdt_value):
            usdt_line = format_usdt(usdt_value)
            if rate is None:
                return f"{usdt_line} (INR conversion unavailable)"
            return f"{usdt_line} (≈ {format_inr(convert_usdt_to_inr(usdt_value, rate))})"

        await update.message.reply_text(
            f"{signal.signal_type} ({signal.quality or 'LOW'} quality)\n"
            f"Regime: {signal.market_regime}\n"
            f"Entry: {level(signal.entry_price)}\n"
            f"SL: {level(signal.stop_loss)}\n"
            f"TP1: {level(signal.take_profit_1)}\n"
            f"{signal.reasoning}"
        )

    async def _cmd_position(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        async with async_session() as session:
            account = await get_or_create_default_account(session)
            trades = await get_open_trades(session, account_id=account.id)
        if not trades:
            await update.message.reply_text("No open positions.")
            return
        lines = [f"{t.side} {t.quantity} @ {format_inr(t.entry_price)} ({t.status})" for t in trades]
        await update.message.reply_text("Open positions:\n" + "\n".join(lines))

    async def _cmd_trades(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        async with async_session() as session:
            account = await get_or_create_default_account(session)
            stats = await get_user_actual_performance(session, account_id=account.id)
        if stats["total_trades"] == 0:
            await update.message.reply_text("No trades logged yet.")
            return
        await update.message.reply_text(
            f"Trades: {stats['total_trades']} (W:{stats['winning_trades']} / L:{stats['losing_trades']})\n"
            f"Total P&L: {format_inr(stats['total_pnl'], show_sign=True)}"
        )

    async def _cmd_performance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        async with async_session() as session:
            account = await get_or_create_default_account(session)
            stats = await get_user_actual_performance(session, account_id=account.id)
        if stats["total_trades"] == 0:
            await update.message.reply_text("No trades logged yet -- nothing to report.")
            return
        win_rate = f"{stats['win_rate']*100:.1f}%" if stats["win_rate"] is not None else "N/A"
        pf = f"{stats['profit_factor']:.2f}" if stats["profit_factor"] is not None else "N/A"
        await update.message.reply_text(
            f"Your actual trading performance:\n"
            f"Total P&L: {format_inr(stats['total_pnl'], show_sign=True)}\n"
            f"Trades: {stats['total_trades']}  Win rate: {win_rate}\n"
            f"Profit factor: {pf}"
        )

    async def _cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        async with async_session() as session:
            account = await get_or_create_default_account(session)
            breakdown = await get_pnl_breakdown(session, account.id, period="daily")
        today_key = period_key(datetime.utcnow(), "daily")
        row = next((r for r in breakdown if r["period"] == today_key), None)
        if row is None:
            await update.message.reply_text("No trades closed today.")
            return
        await update.message.reply_text(
            f"Today's P&L: {format_inr(row['net'], show_sign=True)} net "
            f"({row['trades']} trades, {format_inr(row['fees'])} fees)"
        )

    async def _cmd_risk(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        async with async_session() as session:
            engine = await load_risk_engine(session)
        s = engine.get_status()
        await update.message.reply_text(
            f"Risk status: {s['risk_status']}\n"
            f"Daily P&L: {s['current_daily_pnl_pct']}%  (limit {s['max_daily_loss_pct']}%)\n"
            f"Drawdown: {s['current_drawdown_pct']}%  (hard kill at {s['max_drawdown_pct']}%)\n"
            f"Consecutive losses: {s['consecutive_losses']}\n"
            f"Trades today: {s['trades_today']}/{s['max_daily_trades']}"
        )

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        async with async_session() as session:
            await set_signal_generation_paused(session, True)
        await update.message.reply_text("⏸️ On-demand signal generation paused.")

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        async with async_session() as session:
            await set_signal_generation_paused(session, False)
        await update.message.reply_text("▶️ Signal generation resumed.")


if __name__ == "__main__":
    bot = TelegramBot()
    if not bot.bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set -- see .env.example")
    app = bot.build_app()
    app.run_polling()
