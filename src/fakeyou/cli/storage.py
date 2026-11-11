from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.cli.output import emit_json
from fakeyou.config.settings import Settings
from fakeyou.persistence.segments import ensure_bucket


async def initialize_storage(settings: Settings) -> None:
    if not settings.storage.configured:
        raise FakeYouError(
            ErrorCode.CONFIG_INVALID,
            reason="set FAKEYOU_STORAGE__BUCKET before creating the storage bucket",
        )
    created = await ensure_bucket(settings)
    emit_json({"bucket": settings.storage.bucket, "created": created})
