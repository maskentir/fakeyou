class ApiException(Exception):
    status: int | None
