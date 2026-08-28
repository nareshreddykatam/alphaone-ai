"""Phase 4, section 55: "DO NOT IMPLEMENT AUTOMATIC TRADING. This rule
overrides everything else... AlphaOne must remain: READ-ONLY + MANUAL
EXECUTION." This is the explicit security test the spec requires: prove,
by introspection, that no exchange-integration class anywhere in the
codebase has a callable that could place, cancel, or modify an order, or
change leverage/margin. This must keep passing as new exchange code is
added -- if it ever fails, that is a serious regression, not a false
positive to silence.
"""
import inspect

import pytest

from services.exchange.base import ExchangeMarketDataProvider, ExchangeAccountProvider
from services.exchange.suncrypto import SunCryptoMarketDataProvider, SunCryptoReadOnlyAccountProvider
from services.exchange.coindcx import CoinDCXMarketDataProvider, CoinDCXReadOnlyAccountProvider
from services.market_data.coindcx_ws import CoinDCXMarketDataWebSocket
from services.market_data.live_state import _StartupRetrySupervisor
from services.signal_engine.live_breakout import LiveCandleAggregator
from services.paper_trader.engine import PaperTrader

FORBIDDEN_SUBSTRINGS = (
    "place_order", "create_order", "submit_order", "send_order",
    "cancel_order", "cancel_all", "modify_order", "amend_order", "edit_order",
    "close_position", "close_order",
    "set_leverage", "change_leverage", "update_leverage",
    "set_margin", "change_margin", "adjust_margin",
    "add_margin", "remove_margin", "margin_type",
    "withdraw_funds", "transfer_funds", "withdraw", "deposit_funds",
)

# Non-mutating lifecycle methods that legitimately don't start with "get_"
# (closing an HTTP client is not a trading action).
ALLOWED_NON_GET_METHODS = {"close"}

ACCOUNT_PROVIDER_CLASSES = [
    ExchangeAccountProvider,
    SunCryptoReadOnlyAccountProvider,
    CoinDCXReadOnlyAccountProvider,
]

EXCHANGE_CLASSES = [
    ExchangeMarketDataProvider,
    ExchangeAccountProvider,
    SunCryptoMarketDataProvider,
    SunCryptoReadOnlyAccountProvider,
    CoinDCXMarketDataProvider,
    CoinDCXReadOnlyAccountProvider,
    CoinDCXMarketDataWebSocket,
    _StartupRetrySupervisor,
    LiveCandleAggregator,
]


def _all_public_method_names(cls) -> list[str]:
    return [
        name for name, _ in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    ]


def test_no_exchange_class_exposes_an_order_mutating_method():
    for cls in EXCHANGE_CLASSES:
        method_names = _all_public_method_names(cls)
        for name in method_names:
            lowered = name.lower()
            for forbidden in FORBIDDEN_SUBSTRINGS:
                assert forbidden not in lowered, (
                    f"{cls.__name__}.{name} looks like an order-mutating method "
                    f"(matched forbidden term {forbidden!r}) -- AlphaOne must remain read-only."
                )


@pytest.mark.asyncio
async def test_suncrypto_account_provider_is_read_only_stub_not_a_live_connection():
    """Extra-explicit: the account provider's methods must all report
    unavailability, not fabricate a connection that doesn't exist."""
    provider = SunCryptoReadOnlyAccountProvider()
    status = await provider.get_connection_status()
    assert status["status"] == "UNAVAILABLE"

    balance = await provider.get_balance()
    assert balance["status"] == "UNAVAILABLE"
    assert balance["balance"] is None


def test_exchange_interfaces_only_expose_read_methods_by_name():
    """Whitelist check (belt-and-suspenders alongside the blacklist above):
    every method on every account provider -- the shared interface AND
    each concrete exchange implementation, including any extra
    CoinDCX-specific read methods -- must be a 'get_' read accessor (a
    handful of documented non-trading lifecycle methods like `close` are
    exempt)."""
    for cls in ACCOUNT_PROVIDER_CLASSES:
        for name in _all_public_method_names(cls):
            if name in ALLOWED_NON_GET_METHODS:
                continue
            assert name.startswith("get_"), f"{cls.__name__}.{name} is not a read-only accessor"


@pytest.mark.asyncio
async def test_coindcx_account_provider_is_read_only_and_safe_without_credentials():
    """Extra-explicit, mirroring the SunCrypto check above: with no
    credentials configured, every method must report NOT_CONFIGURED /
    empty rather than raising or fabricating live account data."""
    provider = CoinDCXReadOnlyAccountProvider()
    status = await provider.get_connection_status()
    assert status["status"] == "NOT_CONFIGURED"

    balance = await provider.get_balance()
    assert balance["status"] == "NOT_CONFIGURED"
    assert balance["total_equity"] is None

    assert await provider.get_open_positions() == []
    assert await provider.get_trade_history() == []


def test_market_data_websocket_is_never_constructed_with_credentials():
    """The live public market-data WebSocket (services/market_data/
    coindcx_ws.py) takes no api_key/api_secret at all -- it is
    architecturally incapable of joining the authenticated "coindcx"
    account channel (df-position-update/df-order-update/balance-update),
    let alone placing an order. Verified by introspecting its constructor
    signature rather than trusting a docstring."""
    signature = inspect.signature(CoinDCXMarketDataWebSocket.__init__)
    assert "api_key" not in signature.parameters
    assert "api_secret" not in signature.parameters


def test_startup_retry_supervisor_takes_no_credentials_and_only_wraps_connect():
    """The production-hardening startup-retry supervisor
    (services/market_data/live_state.py) only ever calls the connect_fn
    it's given (market_ws.connect, itself credential-free per the test
    above) -- it takes no api_key/api_secret of its own and exposes no
    method beyond start/stop/is_running."""
    signature = inspect.signature(_StartupRetrySupervisor.__init__)
    assert "api_key" not in signature.parameters
    assert "api_secret" not in signature.parameters
    method_names = _all_public_method_names(_StartupRetrySupervisor)
    assert set(method_names) <= {"is_running", "start", "stop"}


def test_live_candle_aggregator_takes_no_credentials_and_has_no_exchange_calls():
    """The live/intrabar breakout aggregator (services/signal_engine/
    live_breakout.py) is pure in-memory OHLC bucketing -- it never talks
    to CoinDCX or any exchange at all, takes no credentials, and exposes
    only on_tick (plus the dataclass-like `current` attribute)."""
    signature = inspect.signature(LiveCandleAggregator.__init__)
    assert "api_key" not in signature.parameters
    assert "api_secret" not in signature.parameters
    method_names = _all_public_method_names(LiveCandleAggregator)
    assert set(method_names) == {"on_tick"}


def test_ai_trading_v1_modules_expose_no_order_mutating_function_or_method():
    """AI Trading V1: the new AI orchestrator, paper-trading engine, model
    monitor, and multi-coin scanner must all pass the exact same
    forbidden-substring scan as every pre-existing exchange class --
    "paper trading" must never quietly grow a real order-placement path."""
    import services.paper_trader.engine as paper_engine_module
    import services.paper_trader.persistence as paper_persistence_module
    import services.signal_engine.ai_orchestrator as orchestrator_module
    import services.model_monitor.monitor as monitor_module
    import services.scanner.multi_coin as scanner_module
    import services.scheduler.jobs as jobs_module
    import services.telegram_signals.parser as tg_parser_module
    import services.telegram_signals.ingestion as tg_ingestion_module
    import services.telegram_signals.paper_execution as tg_execution_module
    import services.risk_engine.fixed_margin as fixed_margin_module

    modules = [
        paper_engine_module, paper_persistence_module, orchestrator_module,
        monitor_module, scanner_module, jobs_module,
        tg_parser_module, tg_ingestion_module, tg_execution_module, fixed_margin_module,
    ]
    classes = [PaperTrader]

    for module in modules:
        function_names = [
            name for name, obj in inspect.getmembers(module, predicate=inspect.isfunction)
            if not name.startswith("_") and obj.__module__ == module.__name__
        ]
        for name in function_names:
            lowered = name.lower()
            for forbidden in FORBIDDEN_SUBSTRINGS:
                assert forbidden not in lowered, f"{module.__name__}.{name} looks like an order-mutating function"

    for cls in classes:
        for name in _all_public_method_names(cls):
            lowered = name.lower()
            for forbidden in FORBIDDEN_SUBSTRINGS:
                assert forbidden not in lowered, f"{cls.__name__}.{name} looks like an order-mutating method"


def test_telegram_signal_pipeline_never_imports_coindcx_order_client():
    """Phase 36's explicit no-order-placement guarantee: External Telegram
    -> parser -> ingestion -> risk engine -> paper execution must never
    import anything from services.exchange.coindcx (the ONLY module in
    this codebase that talks to CoinDCX's authenticated order endpoints)."""
    import services.telegram_signals.parser as parser_module
    import services.telegram_signals.ingestion as ingestion_module
    import services.telegram_signals.paper_execution as execution_module
    import services.risk_engine.fixed_margin as fixed_margin_module

    for module in (parser_module, ingestion_module, execution_module, fixed_margin_module):
        source = inspect.getsource(module)
        assert "services.exchange.coindcx" not in source, f"{module.__name__} must never import the CoinDCX order client"
        assert "CoinDCXReadOnlyAccountProvider" not in source


# Every mutating Telethon TelegramClient method -- confirmed by direct
# introspection of the installed telethon.TelegramClient (see the audit
# performed while building this module): send_message, send_file,
# forward_messages, edit_message, edit_admin, edit_permissions,
# edit_folder, edit_2fa, delete_messages, delete_dialog,
# send_read_acknowledge. send_code_request/sign_in (the login-flow calls)
# are deliberately included too -- those must exist ONLY in the separate,
# human-run services/telegram_mtproto/setup_session.py, never in the
# listener itself.
TELETHON_MUTATING_METHODS = (
    "send_message", "send_file", "send_album", "forward_messages", "edit_message", "edit_admin",
    "edit_permissions", "edit_folder", "edit_2fa", "delete_messages", "delete_dialog",
    "send_read_acknowledge",
)


def test_mtproto_listener_never_calls_a_mutating_telethon_method():
    """Multi-Coin AI Futures System: the MTProto listener (services/
    telegram_mtproto/client.py) must remain strictly read-only -- connect,
    is_user_authorized, get_entity, and add_event_handler are the only
    TelegramClient calls it's allowed to make."""
    import services.telegram_mtproto.client as mtproto_module
    source = inspect.getsource(mtproto_module)
    for method in TELETHON_MUTATING_METHODS:
        assert f".{method}(" not in source, f"telegram_mtproto/client.py must never call TelegramClient.{method}()"
    assert "services.exchange.coindcx" not in source
    assert "CoinDCXReadOnlyAccountProvider" not in source


def test_mtproto_login_flow_is_isolated_to_the_manual_setup_script():
    """send_code_request/sign_in (the interactive login calls) must exist
    ONLY in the separate, human-run setup_session.py -- never in the
    listener client.py that a server process runs unattended."""
    import services.telegram_mtproto.client as mtproto_module
    source = inspect.getsource(mtproto_module)
    assert ".sign_in(" not in source
    assert ".send_code_request(" not in source
    assert "client.start(" not in source  # Telethon's interactive login convenience call


def test_mtproto_setup_script_never_persists_or_logs_the_session_string():
    """The one-time human-run setup script must only ever print the
    session string directly to the terminal (for the human to copy) --
    never write it to a file, never pass it to a logger."""
    import services.telegram_mtproto.setup_session as setup_module
    source = inspect.getsource(setup_module)
    assert "logger" not in source.lower()
    assert "open(" not in source  # never writes any file
    assert ".write(" not in source


def test_pipeline_never_imports_coindcx_order_client():
    import services.telegram_signals.pipeline as pipeline_module
    source = inspect.getsource(pipeline_module)
    assert "services.exchange.coindcx" not in source
    assert "CoinDCXReadOnlyAccountProvider" not in source


def test_telegram_status_router_is_read_only_and_never_imports_order_client():
    """GET /api/v1/telegram/mtproto-status (apps/api/routers/telegram_status.py)
    is pure observability -- must never import the CoinDCX order client
    and must not define a POST/PUT/PATCH/DELETE handler anywhere."""
    import apps.api.routers.telegram_status as status_module
    source = inspect.getsource(status_module)
    assert "services.exchange.coindcx" not in source
    assert "CoinDCXReadOnlyAccountProvider" not in source
    for forbidden in ("@router.post", "@router.put", "@router.patch", "@router.delete"):
        assert forbidden not in source


def test_paper_trader_never_imports_a_real_exchange_order_client():
    """The paper-trading engine must never import anything from
    services.exchange.coindcx beyond what read-only market context needs
    -- it should have NO import of that module at all, since paper trading
    never needs to talk to the real exchange."""
    import services.paper_trader.engine as paper_engine_module
    source = inspect.getsource(paper_engine_module)
    assert "services.exchange.coindcx" not in source
    assert "import httpx" not in source  # no direct network calls from the paper engine


def test_live_execution_modules_expose_no_order_mutating_function_or_method():
    """Live Futures Auto-Trading V1: every services/live_execution/* module
    and the read-only status router must pass the exact same forbidden-
    substring scan as every pre-existing exchange/paper-trading module --
    building the real safety architecture must never introduce a real
    order-mutating capability by accident."""
    import services.live_execution.kill_switch as kill_switch_module
    import services.live_execution.idempotency as idempotency_module
    import services.live_execution.gates as gates_module
    import services.live_execution.order_client as order_client_module
    import services.live_execution.executor as executor_module
    import services.live_execution.reconciliation as reconciliation_module
    import services.live_execution.daily_loss as daily_loss_module
    import apps.api.routers.live_execution_status as status_module

    modules = [
        kill_switch_module, idempotency_module, gates_module, order_client_module,
        executor_module, reconciliation_module, daily_loss_module, status_module,
    ]
    for module in modules:
        function_names = [
            name for name, obj in inspect.getmembers(module, predicate=inspect.isfunction)
            if not name.startswith("_") and obj.__module__ == module.__name__
        ]
        for name in function_names:
            if name == "submit_futures_order":
                continue  # the one function that WOULD submit an order -- always raises, see its own test coverage below
            lowered = name.lower()
            for forbidden in FORBIDDEN_SUBSTRINGS:
                assert forbidden not in lowered, f"{module.__name__}.{name} looks like an order-mutating function"


def test_order_client_submit_futures_order_always_raises_and_never_makes_a_network_call():
    """The one function in this codebase capable of reaching a real order
    endpoint must be structurally incapable of doing so today: it must
    always raise, unconditionally, and its source must contain no HTTP
    client call of any kind."""
    from services.live_execution.order_client import submit_futures_order, OrderContractNotVerifiedError
    source = inspect.getsource(submit_futures_order)
    for forbidden in ("httpx", "requests.", ".post(", ".get(", "aiohttp"):
        assert forbidden not in source
    with pytest.raises(OrderContractNotVerifiedError):
        submit_futures_order(instrument="B-BTC_USDT", side="buy", quantity=0.001, leverage=10)


def test_live_execution_gates_module_never_imports_a_real_order_client():
    import services.live_execution.gates as gates_module
    source = inspect.getsource(gates_module)
    assert "services.exchange.coindcx" not in source
    assert "submit_futures_order" not in source


def test_live_execution_status_router_is_read_only_and_never_imports_order_client():
    """GET /api/v1/live-execution/status must remain pure observability --
    must never import the order-submission function and must define no
    POST/PUT/PATCH/DELETE handler."""
    import apps.api.routers.live_execution_status as status_module
    source = inspect.getsource(status_module)
    assert "submit_futures_order" not in source
    assert "services.exchange.coindcx" not in source
    for forbidden in ("@router.post", "@router.put", "@router.patch", "@router.delete"):
        assert forbidden not in source


def test_live_execution_executor_only_place_that_imports_the_order_client():
    """Confirms the order-submission function is wired into exactly the
    one place the architecture intends (the executor's terminal step,
    itself gated by ORDER_CONTRACT_VERIFIED which can never pass) -- not
    duplicated or reachable from the Telegram pipeline, the scanner, or
    any router."""
    import services.telegram_signals.pipeline as pipeline_module
    import services.telegram_signals.paper_execution as tg_execution_module
    import services.scanner.multi_coin as scanner_module
    import apps.api.routers.live_execution_status as status_module

    for module in (pipeline_module, tg_execution_module, scanner_module, status_module):
        source = inspect.getsource(module)
        assert "submit_futures_order" not in source
        assert "services.live_execution.order_client" not in source


def test_live_breakout_job_and_evaluator_are_read_only_by_name():
    """services/signal_engine/live_breakout.py's module-level functions
    must also pass the same forbidden-substring scan as every exchange
    class above -- they orchestrate reads (candles, live price) and a DB
    write of a Signal row, never an exchange call of any kind."""
    import services.signal_engine.live_breakout as live_breakout_module

    function_names = [
        name for name, obj in inspect.getmembers(live_breakout_module, predicate=inspect.isfunction)
        if not name.startswith("_")
    ]
    assert "evaluate_live_breakout" in function_names
    for name in function_names:
        lowered = name.lower()
        for forbidden in FORBIDDEN_SUBSTRINGS:
            assert forbidden not in lowered, f"live_breakout.{name} looks like an order-mutating function"
