from __future__ import annotations

import array
import json
import os
import selectors
import socket
import struct
import sys
from pathlib import Path
from typing import TextIO

from evals.writer_spike.pulse import now_ns

FRAME_HEADER = struct.Struct(">IIIII")
ANCILLARY_BYTES = socket.CMSG_SPACE(64 * array.array("i").itemsize)


def _descriptors(ancillary: list[tuple[int, int, bytes]]) -> list[int]:
    found: list[int] = []
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            values = array.array("i")
            values.frombytes(data[: len(data) - (len(data) % values.itemsize)])
            found.extend(values)
    return found


def _exactly(source: socket.socket, size: int) -> tuple[bytes, list[int]]:
    data = bytearray()
    descriptors: list[int] = []
    while len(data) < size:
        chunk, ancillary, _flags, _address = source.recvmsg(size - len(data), ANCILLARY_BYTES)
        if not chunk:
            return b"", descriptors
        data += chunk
        descriptors.extend(_descriptors(ancillary))
    return bytes(data), descriptors


def _send(target: socket.socket, data: bytes, descriptors: list[int]) -> None:
    rights = (
        [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", descriptors))]
        if descriptors
        else []
    )
    target.sendmsg([data], rights)
    for descriptor in descriptors:
        os.close(descriptor)


def _forward(source: socket.socket, target: socket.socket, direction: str, log: TextIO) -> bool:
    header, descriptors = _exactly(source, FRAME_HEADER.size)
    received = now_ns()
    if not header:
        return False
    payload_bytes = FRAME_HEADER.unpack(header)[0]
    _send(target, header, descriptors)
    passed = len(descriptors)
    if payload_bytes:
        payload, more = _exactly(source, payload_bytes)
        if not payload:
            return False
        _send(target, payload, more)
        passed += len(more)
    log.write(
        json.dumps(
            {
                "at": received,
                "direction": direction,
                "bytes": FRAME_HEADER.size + payload_bytes,
                "fds": passed,
            }
        )
        + "\n"
    )
    log.flush()
    return True


def serve(listen: Path, upstream: Path, log: TextIO) -> None:
    listen.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(listen))
    server.listen(4)
    sys.stdout.write("ready\n")
    sys.stdout.flush()
    while True:
        client, _address = server.accept()
        daemon = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        daemon.connect(str(upstream))
        selector = selectors.DefaultSelector()
        selector.register(client, selectors.EVENT_READ, ("client_to_server", daemon))
        selector.register(daemon, selectors.EVENT_READ, ("server_to_client", client))
        alive = True
        while alive:
            for key, _events in selector.select():
                direction, target = key.data
                source = key.fileobj
                if not isinstance(source, socket.socket) or not _forward(
                    source, target, direction, log
                ):
                    alive = False
                    break
        selector.close()
        client.close()
        daemon.close()


if __name__ == "__main__":
    with Path(sys.argv[3]).open("a") as journal:
        serve(Path(sys.argv[1]), Path(sys.argv[2]), journal)
