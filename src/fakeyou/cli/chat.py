from fakeyou.cli.client import ClientSettings, admin_client
from fakeyou.cli.output import emit_text
from fakeyou.config.identifiers import verify_chat_thread_id


async def chat_via_api(
    *, message: str, thread: str, memory: bool, settings: ClientSettings
) -> None:
    verify_chat_thread_id(thread)
    async with admin_client(settings) as client:
        emit_text(await client.chat_turn(thread_id=thread, message=message, memory=memory))
