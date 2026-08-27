from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.config import get_settings
from database.schema import get_db
from database.schema.models import Candle, Signal, Trade, TradeStatus, TradeSource
from services.exchange.coindcx import CoinDCXReadOnlyAccountProvider
from services.exchange.fx import get_usdt_inr_rate, convert_usdt_to_inr, conversion_meta
from services.exchange.sync import get_last_sync_event, is_stale
from services.market_data.live_state import market_ws
from services.portfolio.account import get_or_create_default_account
from services.portfolio.service import get_pnl_breakdown, period_key
from services.risk_engine.state_store import load_risk_engine

router = APIRouter()
settings = get_settings()


@router.get("/")
async def get_dashboard(db: AsyncSession = Depends(get_db)):
    account = await get_or_create_default_account(db)

    latest_candle = (await db.execute(
        select(Candle).where(Candle.symbol == "BTC/USDT").order_by(Candle.timestamp.desc()).limit(1)
    )).scalar_one_or_none()

    latest_signal = (await db.execute(
        select(Signal).order_by(Signal.timestamp.desc()).limit(1)
    )).scalar_one_or_none()

    today_key = period_key(datetime.utcnow(), "daily")
    breakdown = await get_pnl_breakdown(db, account.id, period="daily")
    today_row = next((r for r in breakdown if r["period"] == today_key), None)

    price_age_minutes = None
    if latest_candle is not None:
        price_age_minutes = (datetime.utcnow() - latest_candle.timestamp).total_seconds() / 60

    # Open exchange-synced positions (kept fresh by the CoinDCX sync job,
    # not called live here -- avoids hammering the API on every dashboard poll).
    open_positions_result = await db.execute(
        select(Trade).where(
            Trade.account_id == account.id, Trade.source == TradeSource.COINDCX_SYNC.value,
            Trade.status.in_([TradeStatus.OPEN.value, TradeStatus.PARTIALLY_CLOSED.value]),
        )
    )
    open_positions = open_positions_result.scalars().all()
    unrealized_pnl = sum((p.unrealized_pnl or 0) for p in open_positions) if open_positions else None

    sync_event = await get_last_sync_event(db)
    # Never trust a stale/legacy stored connection_status alone (e.g. an
    # account row created before Phase 5) -- check whether credentials are
    # actually configured right now, which is the ground truth.
    provider = CoinDCXReadOnlyAccountProvider(settings.coindcx_api_key, settings.coindcx_api_secret)
    if not provider.is_configured:
        account_data_source = "NOT_CONFIGURED"
    elif account.connection_status == "LIVE" and not is_stale(sync_event):
        account_data_source = "LIVE"
    elif account.connection_status == "LIVE":
        account_data_source = "STALE"
    else:
        account_data_source = "DISCONNECTED"

    risk_engine = await load_risk_engine(db)

    # BTC price: prefer the live CoinDCX WebSocket (services/market_data/
    # coindcx_ws.py) when it has ever delivered a price; otherwise fall back
    # to the pre-existing Binance-ingested-candle path (Phases 1-3), which
    # is what every dashboard test still exercises since
    # MARKET_DATA_WS_ENABLED defaults to False and no WS is ever connected
    # in tests. Both paths are USDT-denominated -- convert to INR for
    # display. unrealized_pnl/daily_pnl come from the INR-margined CoinDCX
    # account / INR trade journal and are already native INR, so they are
    # never run through this conversion.
    live_tick = market_ws.state
    live_status = market_ws.connection_status()
    if live_tick.last_price_usdt is not None:
        btc_price_usdt = live_tick.last_price_usdt
        btc_price_source = live_status.value
        market_data_source = "CoinDCX WebSocket"
        btc_price_updated_at = live_tick.received_at
    else:
        btc_price_usdt = latest_candle.close if latest_candle else None
        btc_price_source = "SYNCED" if (price_age_minutes is not None and price_age_minutes < 120) else (
            "STALE" if latest_candle else "UNAVAILABLE"
        )
        market_data_source = "Binance (historical candle ingestion)"
        btc_price_updated_at = latest_candle.timestamp if latest_candle else None

    needs_rate = btc_price_usdt is not None or latest_signal is not None
    rate = await get_usdt_inr_rate() if needs_rate else None
    btc_price_inr = convert_usdt_to_inr(btc_price_usdt, rate)

    return {
        "exchange": "COINDCX",
        "trading_mode": settings.trading_mode,
        "account_connection_status": account.connection_status,
        "account_data_source": account_data_source,
        "last_synced_at": account.last_synced_at,
        "btc_price_inr": btc_price_inr,
        "btc_price_usdt": btc_price_usdt,
        "btc_price_source": btc_price_source,
        "btc_price_updated_at": btc_price_updated_at,
        "market_data_source": market_data_source,
        "market_data_status": live_status.value,
        "market_data_mark_price_usdt": live_tick.mark_price_usdt,
        **conversion_meta(rate),
        "current_signal": latest_signal.signal_type if latest_signal else None,
        "signal_quality": latest_signal.quality if latest_signal else None,
        "market_regime": latest_signal.market_regime if latest_signal else None,
        "signal_entry_price_inr": convert_usdt_to_inr(latest_signal.entry_price, rate) if latest_signal else None,
        "signal_stop_loss_inr": convert_usdt_to_inr(latest_signal.stop_loss, rate) if latest_signal else None,
        "signal_take_profit_1_inr": convert_usdt_to_inr(latest_signal.take_profit_1, rate) if latest_signal else None,
        "signal_risk_reward": latest_signal.risk_reward if latest_signal else None,
        "open_positions": len(open_positions),
        "unrealized_pnl": unrealized_pnl,
        "daily_pnl": today_row["net"] if today_row else 0.0,
        "daily_pnl_source": "SYNCED" if today_row else "MANUAL",
        "risk_status": risk_engine.get_risk_status().value,
        "telegram_enabled": settings.telegram_enabled,
    }
