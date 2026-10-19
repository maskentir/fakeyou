import hashlib
import json
from typing import Literal

DigestAlgorithm = Literal["sha1", "sha256"]


def canonical_json_digest(
    payload: object,
    *,
    algorithm: DigestAlgorithm = "sha256",
    ensure_ascii: bool = True,
    compact: bool = False,
) -> str:
    separators = (",", ":") if compact else None
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=ensure_ascii, separators=separators)
    return hashlib.new(algorithm, encoded.encode("utf-8")).hexdigest()
