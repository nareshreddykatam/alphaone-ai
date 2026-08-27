from apps.api.config import get_settings

settings = get_settings()


async def get_redis():
    import redis.asyncio as aioredis
    client = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        yield client
    finally:
        await client.close()
