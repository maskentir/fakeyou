from typing import Any

import numpy as np
from numpy.typing import NDArray

class SessionOptions:
    inter_op_num_threads: int
    intra_op_num_threads: int

class InferenceSession:
    def __init__(
        self,
        path_or_bytes: str,
        sess_options: SessionOptions | None = None,
        providers: list[str] | None = None,
    ) -> None: ...
    def run(
        self, output_names: None, input_feed: dict[str, NDArray[np.generic] | np.generic]
    ) -> list[Any]: ...
