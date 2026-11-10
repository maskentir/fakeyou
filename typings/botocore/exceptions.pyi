from typing import Any

class ClientError(Exception):
    response: dict[str, Any]
