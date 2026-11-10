from contextlib import AbstractAsyncContextManager
from typing import Any

class Session:
    def __init__(
        self,
        aws_access_key_id: str | None = ...,
        aws_secret_access_key: str | None = ...,
        aws_session_token: str | None = ...,
        region_name: str | None = ...,
        profile_name: str | None = ...,
    ) -> None: ...
    def client(
        self, service_name: str, *, endpoint_url: str | None = ...
    ) -> AbstractAsyncContextManager[Any]: ...
