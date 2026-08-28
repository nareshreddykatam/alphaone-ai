"""Read-only observability for the MTProto external-signal listener
(services/telegram_mtproto/client.py). GET only -- no way to change
configuration, start/stop the listener, or act on the channel through
this router. Every field is real, live-computed operational state; never
a credential value (see MTProtoStatus's own docstring and the dedicated
security tests in tests/unit/test_telegram_status_endpoint.py)."""
from fastapi import APIRouter

router = APIRouter()


@router.get("/mtproto-status")
async def get_mtproto_status():
    from services.telegram_mtproto.client import mtproto_listener

    status = mtproto_listener.get_status()
    return {
        "enabled": status.enabled,
        "connected": status.connected,
        "authorized": status.authorized,
        "channel_configured": status.channel_configured,
        "authorized_channel": status.authorized_channel,
        "resolved_channel_id": status.resolved_channel_id,
        "listener_running": status.listener_running,
        "last_event_at": status.last_event_at.isoformat() if status.last_event_at else None,
        "last_event_type": status.last_event_type,
        "seconds_since_last_event": status.seconds_since_last_event,
    }
