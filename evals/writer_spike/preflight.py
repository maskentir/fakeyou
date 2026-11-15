from __future__ import annotations

import array
import hashlib
import json
import os
import platform
import random
import signal
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

from evals.writer_spike.observer import MIN_ATTRIBUTABLE_BYTES, Recording, load
from evals.writer_spike.pulse import (
    FRAME_BYTES,
    FRAME_SECONDS,
    RATE,
    SAMPLE_BYTES,
    SEEK_ABSOLUTE,
    SEEK_RELATIVE,
    SEEK_RELATIVE_ON_READ,
    Client,
    Playback,
    PulseError,
    now_ns,
)

OUT = Path(os.environ.get("SPIKE_OUT", "/out"))
RUNTIME = Path(os.environ["XDG_RUNTIME_DIR"])
NATIVE_SOCKET = RUNTIME / "pulse" / "native"
RELAY_SOCKET = Path("/tmp/writer-spike-relay.sock")
SOCKET_ONLY_CONFIG = Path("/tmp/writer-spike-socket-only.conf")
CLIENT_CONFIG_VARIABLE = "PULSE_CLIENTCONFIG"
EXPECTED_MODULES = (
    "module-native-protocol-unix",
    "module-null-sink",
    "module-null-sink",
    "module-remap-source",
)
REMAP_OWNER = "module-remap-source.c"
NON_BLOCKING_NS = 5_000_000
STOP_CONFIRM_SECONDS = 0.5
CLIP_LIBRARY = 100_000
SECOND_BYTES = RATE * SAMPLE_BYTES
LEAD_BYTES = FRAME_BYTES * 2
FIRST_FRAME_ALLOWANCE_NS = 250_000_000
HOLD_SECONDS = 0.5


def clip(clip_id: int, size: int = FRAME_BYTES) -> bytes:
    samples = array.array("h")
    samples.frombytes(hashlib.shake_256(clip_id.to_bytes(8, "little")).digest(size))
    for position, sample in enumerate(samples):
        if sample == 0:
            samples[position] = 1
    return samples.tobytes()


def pactl(*arguments: str) -> str:
    return subprocess.run(
        ("pactl", *arguments), check=True, capture_output=True, text=True, timeout=10
    ).stdout


def daemon_pid() -> int:
    return int((RUNTIME / "pulse" / "pid").read_text().strip())


def thread_states(pid: int) -> list[str]:
    states: list[str] = []
    for task in Path(f"/proc/{pid}/task").iterdir():
        states.append((task / "stat").read_text().rsplit(")", 1)[1].split()[0])
    return states


def stop_daemon(pid: int) -> int | None:
    os.kill(pid, signal.SIGSTOP)
    deadline = time.monotonic() + STOP_CONFIRM_SECONDS
    while time.monotonic() < deadline:
        if all(state == "T" for state in thread_states(pid)):
            return now_ns()
    return None


def resume_daemon(pid: int) -> int:
    os.kill(pid, signal.SIGCONT)
    return now_ns()


def summary(values: list[int]) -> dict[str, float]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0}
    return {
        "count": len(ordered),
        "p50": statistics.median(ordered),
        "p99": ordered[min(len(ordered) - 1, round(len(ordered) * 0.99))],
        "max": ordered[-1],
    }


@contextmanager
def observing(name: str) -> Generator[Callable[[], Recording]]:
    path = OUT / f"{name}.rec"
    process = subprocess.Popen(
        (sys.executable, "-m", "evals.writer_spike.observer", str(path)),
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    if process.stdout.readline().strip() != "ready":
        raise RuntimeError("observer did not start")

    def finish() -> Recording:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=10)
        return load(path)

    try:
        yield finish
    finally:
        if process.poll() is None:
            process.kill()


def pf8_isolation() -> dict[str, object]:
    modules = [line.split("\t")[1] for line in pactl("list", "short", "modules").splitlines()]
    clients = [line.split("\t")[2] for line in pactl("list", "short", "clients").splitlines()]
    inputs = pactl("list", "short", "sink-inputs").splitlines()
    outputs = pactl("list", "short", "source-outputs").splitlines()
    foreign = [client for client in clients if client != "pactl"]
    foreign_outputs = [line for line in outputs if line.split("\t")[3] != REMAP_OWNER]
    return {
        "established": tuple(modules) == EXPECTED_MODULES
        and not foreign
        and not inputs
        and not foreign_outputs,
        "foreign_source_outputs": foreign_outputs,
        "modules": modules,
        "clients": clients,
        "sink_inputs": inputs,
        "source_outputs": outputs,
    }


def _latency_lines(listing: str, name: str) -> list[str]:
    block = listing.split(f"Name: {name}\n", 1)[1].split("\n\n", 1)[0]
    return [line.strip() for line in block.splitlines() if "Latency" in line or "State" in line]


def pf5_latency() -> dict[str, object]:
    idle = {
        "sink": _latency_lines(pactl("list", "sinks"), "agent_output"),
        "monitor": _latency_lines(pactl("list", "sources"), "agent_output.monitor"),
    }
    client = Client("pf5")
    playback = Playback(client, "pf5")
    playback.write(clip(1, SECOND_BYTES // 5))
    playback.request_timing()
    client.pump(0.1)
    streaming = {
        "sink": _latency_lines(pactl("list", "sinks"), "agent_output"),
        "monitor": _latency_lines(pactl("list", "sources"), "agent_output.monitor"),
        "stream_configured_sink_usec": [t.configured_sink_usec for t in playback.timings],
        "stream_sink_usec": [t.sink_usec for t in playback.timings],
    }
    playback.close()
    client.close()
    return {"established": True, "idle": idle, "with_stream": streaming}


def pf6_stop_confirmation(repetitions: int = 200) -> dict[str, object]:
    pid = daemon_pid()
    delays: list[int] = []
    unconfirmed = 0
    not_resumed = 0
    for _ in range(repetitions):
        requested = now_ns()
        confirmed = stop_daemon(pid)
        if confirmed is None:
            unconfirmed += 1
        else:
            delays.append(confirmed - requested)
        resume_daemon(pid)
        time.sleep(0.02)
        if any(state == "T" for state in thread_states(pid)):
            not_resumed += 1
    return {
        "established": unconfirmed == 0 and not_resumed == 0,
        "repetitions": repetitions,
        "unconfirmed": unconfirmed,
        "not_resumed": not_resumed,
        "threads": len(thread_states(pid)),
        "confirm_delay_ns": summary(delays),
    }


def pf9_no_callback_in_section(repetitions: int = 1000) -> dict[str, object]:
    client = Client("pf9")
    playback = Playback(client, "pf9-running")
    staged = Playback(client, "pf9-corked", corked=True)
    frame = clip(9)
    during_write = 0
    during_query = 0
    during_uncork = 0
    iterations = 0
    for _ in range(repetitions):
        client.pump_until(lambda: playback.writable() >= FRAME_BYTES, 1.0)
        before, turns = client.callbacks, client.iterations
        playback.writable()
        during_query += client.callbacks - before
        before = client.callbacks
        playback.write(frame)
        during_write += client.callbacks - before
        before = client.callbacks
        staged.cork(corked=False)
        during_uncork += client.callbacks - before
        iterations += client.iterations - turns
        client.pump(0.002)
        staged.cork(corked=True)
        client.pump(0.002)
    hooks = {name: len(moments) for name, moments in playback.hooks.items()}
    playback.close()
    staged.close()
    client.close()
    return {
        "established": during_write == 0 and during_query == 0 and during_uncork == 0,
        "repetitions": repetitions,
        "callbacks_during_writable_query": during_query,
        "callbacks_during_write": during_write,
        "callbacks_during_uncork_request": during_uncork,
        "mainloop_iterations_in_section": iterations,
        "callbacks_seen_outside_section": hooks,
    }


def _timed_writes(playback: Playback, frame: bytes, count: int, *, pump: bool) -> dict[str, object]:
    durations: list[int] = []
    codes: dict[int, int] = {}
    beyond_writable = 0
    for _ in range(count):
        if pump:
            playback.client.pump_until(lambda: playback.writable() >= FRAME_BYTES, 1.0)
        if playback.writable() < FRAME_BYTES:
            beyond_writable += 1
        started = now_ns()
        code = playback.write(frame)
        durations.append(now_ns() - started)
        codes[code] = codes.get(code, 0) + 1
    return {
        "write_ns": summary(durations),
        "return_codes": codes,
        "writes_issued_beyond_writable_size": beyond_writable,
    }


def pf1_write_never_blocks(count: int = 1000) -> dict[str, object]:
    frame = clip(1)
    client = Client("pf1-normal")
    playback = Playback(client, "pf1-normal")
    normal = _timed_writes(playback, frame, count, pump=True)
    playback.close()
    client.close()

    client = Client("pf1-full")
    playback = Playback(client, "pf1-full")
    full = _timed_writes(playback, frame, count, pump=False)
    playback.flush()
    client.pump(0.2)
    playback.close()
    client.close()

    client = Client("pf1-stopped")
    playback = Playback(client, "pf1-stopped")
    pid = daemon_pid()
    confirmed = stop_daemon(pid)
    stopped = _timed_writes(playback, frame, count, pump=False)
    iterate_durations: list[int] = []
    for _ in range(count):
        started = now_ns()
        client.iterate()
        iterate_durations.append(now_ns() - started)
    resume_daemon(pid)
    client.pump(0.2)
    playback.flush()
    client.pump(0.2)
    playback.close()
    client.close()

    worst = max(
        int(section["write_ns"]["max"])  # type: ignore[index]
        for section in (normal, full, stopped)
    )
    return {
        "established": confirmed is not None and worst < NON_BLOCKING_NS,
        "normal": normal,
        "full_buffer_without_pumping": full,
        "daemon_stopped": stopped,
        "daemon_stop_confirmed": confirmed is not None,
        "iterate_ns_while_daemon_stopped": summary(iterate_durations),
    }


def pf2_bytes_leave_only_when_pumped(repetitions: int = 200) -> dict[str, object]:
    journal = OUT / "pf2-relay.jsonl"
    journal.unlink(missing_ok=True)
    relay = subprocess.Popen(
        (
            sys.executable,
            "-m",
            "evals.writer_spike.relay",
            str(RELAY_SOCKET),
            str(NATIVE_SOCKET),
            str(journal),
        ),
        stdout=subprocess.PIPE,
        text=True,
    )
    assert relay.stdout is not None
    if relay.stdout.readline().strip() != "ready":
        raise RuntimeError("relay did not start")
    SOCKET_ONLY_CONFIG.write_text("enable-shm = no\n")
    os.environ[CLIENT_CONFIG_VARIABLE] = str(SOCKET_ONLY_CONFIG)
    try:
        client = Client("pf2", server=f"unix:{RELAY_SOCKET}")
        playback = Playback(client, "pf2")
        client.pump(0.5)
        pending_when_idle = client.pending()
        windows: list[tuple[int, int, int, int]] = []
        pending_after_write = 0
        for index in range(repetitions):
            client.pump(0.1)
            invoked = now_ns()
            playback.write(clip(index))
            returned = now_ns()
            pending_after_write += client.pending()
            time.sleep(0.5)
            pumped = now_ns()
            client.pump_until(lambda: not client.pending(), 1.0)
            windows.append((invoked, returned, pumped, now_ns()))
        client.pump(0.2)
        playback.request_timing()
        client.iterate()
        client.iterate()
        pending_with_reply_outstanding = client.pending()
        client.pump(0.3)
        pending_after_reply = client.pending()
        playback.close()
        client.close()
    finally:
        del os.environ[CLIENT_CONFIG_VARIABLE]
        relay.terminate()
        relay.wait(timeout=5)
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    outbound = [event for event in events if event["direction"] == "client_to_server"]
    early = 0
    early_bytes = 0
    sent_after_pump = 0
    first_after_pump_ns: list[int] = []
    for invoked, _returned, pumped, finished in windows:
        before = [event for event in outbound if invoked <= event["at"] < pumped]
        after = [event for event in outbound if pumped <= event["at"] <= finished + 50_000_000]
        early += bool(before)
        early_bytes += sum(event["bytes"] for event in before)
        if after:
            sent_after_pump += 1
            first_after_pump_ns.append(after[0]["at"] - pumped)
    return {
        "established": early == 0 and sent_after_pump == repetitions,
        "transport": "socket only, shared memory disabled in the client for this check",
        "client_to_server_frames": len(outbound),
        "repetitions": repetitions,
        "held_without_pumping_seconds": 0.5,
        "repetitions_with_bytes_before_first_pump": early,
        "bytes_before_first_pump": early_bytes,
        "repetitions_with_bytes_after_first_pump": sent_after_pump,
        "first_byte_after_pump_ns": summary(first_after_pump_ns),
        "descriptors_passed": sum(event["fds"] for event in events),
        "pending_query": {
            "idle": pending_when_idle,
            "true_after_write_before_pump": pending_after_write,
            "with_reply_outstanding_after_send": pending_with_reply_outstanding,
            "after_reply": pending_after_reply,
        },
    }


def pf2_observed_on_production_transport(repetitions: int = 200) -> dict[str, object]:
    windows: dict[int, tuple[int, int]] = {}
    with observing("pf2") as finish:
        client = Client("pf2-observed")
        playback = Playback(client, "pf2-observed")
        client.pump(3.0)
        for index in range(repetitions):
            client.pump(0.15)
            invoked = now_ns()
            playback.write(clip(index))
            time.sleep(0.5)
            windows[index] = (invoked, now_ns())
            client.pump_until(lambda: not client.pending(), 1.0)
        client.pump(1.0)
        playback.close()
        client.close()
        recording = finish()
    library = b"".join(clip(index) for index in range(repetitions))
    seen_before_pump = 0
    seen = 0
    unattributed = 0
    after_pump: list[int] = []
    for run in recording.runs():
        position = library.find(run.data) if len(run.data) >= MIN_ATTRIBUTABLE_BYTES else -1
        if position < 0 or position % FRAME_BYTES:
            unattributed += 1
            continue
        _invoked, pumped = windows[position // FRAME_BYTES]
        seen += 1
        seen_before_pump += run.first_received_at < pumped
        after_pump.append(run.first_received_at - pumped)
    return {
        "established": seen_before_pump == 0 and seen == repetitions and unattributed == 0,
        "transport": "production: shared memory and shared ring channel",
        "repetitions": repetitions,
        "held_without_pumping_seconds": 0.5,
        "clips_observed": seen,
        "clips_observed_before_first_pump": seen_before_pump,
        "runs_not_attributed": unattributed,
        "first_pump_to_observer_receipt_ns": summary(after_pump),
    }


def setup_stream_start(clips: int = 100) -> dict[str, object]:
    variants: dict[str, tuple[float, int]] = {
        "no_prebuffer_relative_write": (0.0, SEEK_RELATIVE),
        "no_prebuffer_write_at_read_index": (0.0, SEEK_RELATIVE_ON_READ),
        "one_frame_prebuffer_relative_write": (FRAME_SECONDS, SEEK_RELATIVE),
    }
    outcome: dict[str, object] = {}
    for offset, (name, (prebuffer, seek)) in enumerate(variants.items()):
        base = offset * clips
        invoked: dict[int, int] = {}
        with observing(f"setup-{name}") as finish:
            client = Client(name)
            playback = Playback(client, name, prebuffer_seconds=prebuffer)
            client.pump(3.0)
            for index in range(base, base + clips):
                invoked[index] = now_ns()
                playback.write(clip(index), seek)
                client.pump(0.15)
            client.pump(1.0)
            playback.request_timing()
            client.pump(0.1)
            last = playback.timings[-1]
            started = len(playback.hooks["started"])
            underflow = len(playback.hooks["underflow"])
            playback.close()
            client.close()
            recording = finish()
        library = b"".join(clip(index) for index in range(base, base + clips))
        whole = 0
        partial = 0
        latency: list[int] = []
        for run in recording.runs():
            position = library.find(run.data) if len(run.data) >= MIN_ATTRIBUTABLE_BYTES else -1
            if position < 0:
                partial += 1
                continue
            clip_id, inside = divmod(position, FRAME_BYTES)
            if inside == 0 and len(run.data) == FRAME_BYTES:
                whole += 1
                latency.append(run.first_received_at - invoked[base + clip_id])
            else:
                partial += 1
        outcome[name] = {
            "clips_written": clips,
            "clips_observed_whole": whole,
            "runs_partial_or_unattributed": partial,
            "write_invoked_to_observer_receipt_ns": summary(latency),
            "final_read_index": last.read_index,
            "final_write_index": last.write_index,
            "started_notifications": started,
            "underflow_notifications": underflow,
        }
    return outcome


def _placement(playback: Playback, mode: str) -> tuple[int, int]:
    if mode == "at_read_index":
        return SEEK_RELATIVE_ON_READ, 0
    moments = playback.request_timing()
    playback.client.pump_until(lambda: bool(moments), 1.0)
    return SEEK_ABSOLUTE, playback.timings[-1].read_index + LEAD_BYTES


def _late_release(mode: str, scenario: str, base: int, clips: int) -> dict[str, object]:
    pid = daemon_pid()
    marks: dict[int, tuple[int, int | None]] = {}
    unconfirmed = 0
    with observing(f"diag-{mode}-{scenario}") as finish:
        client = Client("diag")
        playback = Playback(client, "diag")
        client.pump(3.0)
        for index in range(base, base + clips):
            client.pump(0.15)
            seek, offset = _placement(playback, mode)
            stopped = scenario == "daemon_stopped_before_release"
            if stopped and stop_daemon(pid) is None:
                unconfirmed += 1
            invoked = now_ns()
            playback.write(clip(index), seek, offset)
            resumed: int | None = None
            if scenario == "pump_held":
                time.sleep(HOLD_SECONDS)
            if stopped:
                client.pump(HOLD_SECONDS)
                resumed = resume_daemon(pid)
            marks[index] = (invoked, resumed)
            client.pump(0.05)
        client.pump(1.0)
        playback.close()
        client.close()
        recording = finish()
    library = b"".join(clip(index) for index in range(base, base + clips))
    observed: dict[int, int] = {}
    first_receipt: dict[int, int] = {}
    unattributed = 0
    for run in recording.runs():
        position = library.find(run.data) if len(run.data) >= MIN_ATTRIBUTABLE_BYTES else -1
        if position < 0:
            unattributed += 1
            continue
        index = base + position // FRAME_BYTES
        observed[index] = observed.get(index, 0) + len(run.data)
        first_receipt.setdefault(index, run.first_received_at)
    whole = sum(size == FRAME_BYTES for size in observed.values())
    confirmed_late = sum(
        resumed is not None and index in observed and resumed - invoked > FIRST_FRAME_ALLOWANCE_NS
        for index, (invoked, resumed) in marks.items()
    )
    return {
        "clips_written": clips,
        "clips_observed_whole": whole,
        "clips_observed_in_part": len(observed) - whole,
        "clips_not_observed": clips - len(observed),
        "runs_not_attributed": unattributed,
        "observed_bytes_per_observed_clip": summary(list(observed.values())),
        "write_invoked_to_observer_receipt_ns": summary(
            [first_receipt[index] - marks[index][0] for index in first_receipt]
        ),
        "observed_with_lower_bound_past_250ms": confirmed_late,
        "daemon_stop_unconfirmed": unconfirmed,
    }


def diag_late_release(clips: int = 50) -> dict[str, object]:
    outcome: dict[str, object] = {
        "lead_bytes_for_absolute_index": LEAD_BYTES,
        "hold_seconds": HOLD_SECONDS,
    }
    base = 0
    for mode in ("at_read_index", "absolute_index"):
        for scenario in ("pumped_at_once", "pump_held", "daemon_stopped_before_release"):
            outcome[f"{mode}/{scenario}"] = _late_release(mode, scenario, base, clips)
            base += clips
    return outcome


def pf3_reply_not_from_stopped_daemon(repetitions: int = 100) -> dict[str, object]:
    pid = daemon_pid()
    client = Client("pf3")
    playback = Playback(client, "pf3")
    replies_while_stopped = 0
    replies_after_resume = 0
    unconfirmed = 0
    write_index_advanced_while_stopped = 0
    reply_delay: list[int] = []
    for index in range(repetitions):
        client.pump(0.05)
        playback.request_timing()
        client.pump(0.05)
        before = playback.timings[-1].write_index if playback.timings else 0
        confirmed = stop_daemon(pid)
        if confirmed is None:
            unconfirmed += 1
            resume_daemon(pid)
            continue
        playback.write(clip(index))
        moments = playback.request_timing()
        client.pump(0.3)
        replies_while_stopped += bool(moments)
        info = playback.timings[-1].write_index if playback.timings else 0
        write_index_advanced_while_stopped += info > before and not moments
        resumed = resume_daemon(pid)
        if client.pump_until(lambda: bool(moments), 2.0):  # noqa: B023
            replies_after_resume += 1
            reply_delay.append(moments[0] - resumed)
    playback.close()
    client.close()
    return {
        "refuted": replies_while_stopped > 0,
        "repetitions": repetitions,
        "stop_unconfirmed": unconfirmed,
        "replies_received_while_daemon_stopped": replies_while_stopped,
        "replies_received_after_resume": replies_after_resume,
        "reply_after_resume_ns": summary(reply_delay),
    }


def _library() -> bytes:
    return b"".join(clip(clip_id) for clip_id in range(CLIP_LIBRARY))


def _occurrences(haystack: bytes, needle: bytes) -> list[int]:
    found: list[int] = []
    position = haystack.find(needle)
    while position >= 0:
        found.append(position)
        position = haystack.find(needle, position + 1)
    return found


def pf7_observer_attribution(full: int = 200, partial: int = 100) -> dict[str, object]:
    chooser = random.Random(7)
    plan: list[tuple[int, int]] = [(clip_id, FRAME_BYTES) for clip_id in range(full)]
    plan += [
        (full + offset, chooser.randrange(1, FRAME_BYTES // SAMPLE_BYTES) * SAMPLE_BYTES)
        for offset in range(partial)
    ]
    chooser.shuffle(plan)
    invoked: dict[int, int] = {}
    with observing("pf7") as finish:
        client = Client("pf7")
        playback = Playback(client, "pf7")
        client.pump(10.0)
        first_write = now_ns()
        for clip_id, size in plan:
            invoked[clip_id] = now_ns()
            playback.write(clip(clip_id)[:size])
            client.pump(0.15)
        client.pump(1.0)
        playback.close()
        client.close()
        recording = finish()
    runs = recording.runs()
    library = _library()
    expected = dict(plan)
    silence_violations = sum(run.first_received_at < first_write for run in runs)
    attributed: dict[int, int] = {}
    misattributed = 0
    ambiguous = 0
    unattributable = 0
    latency: list[int] = []
    for run in runs:
        if len(run.data) < MIN_ATTRIBUTABLE_BYTES:
            unattributable += 1
            continue
        matches = _occurrences(library, run.data)
        if len(matches) != 1:
            ambiguous += 1
            continue
        clip_id, offset = divmod(matches[0], FRAME_BYTES)
        if offset != 0 or expected.get(clip_id) != len(run.data) or clip_id in attributed:
            misattributed += 1
            continue
        attributed[clip_id] = len(run.data)
        latency.append(run.first_received_at - invoked[clip_id])
    short = sum(size < MIN_ATTRIBUTABLE_BYTES for size in expected.values())
    missing = [
        clip_id
        for clip_id, size in expected.items()
        if size >= MIN_ATTRIBUTABLE_BYTES and clip_id not in attributed
    ]
    return {
        "established": silence_violations == 0
        and misattributed == 0
        and ambiguous == 0
        and not missing
        and unattributable == short,
        "library_clips": CLIP_LIBRARY,
        "clips_played_full": full,
        "clips_played_partial": partial,
        "recorded_seconds": len(recording.audio) / SECOND_BYTES,
        "fragments": len(recording.starts),
        "nonzero_samples_in_silence": silence_violations,
        "runs": len(runs),
        "attributed_exactly": len(attributed),
        "misattributed": misattributed,
        "ambiguous": ambiguous,
        "missing": len(missing),
        "runs_too_short_to_attribute": unattributable,
        "clips_too_short_to_attribute": short,
        "write_invoked_to_observer_receipt_ns": summary(latency),
    }


def pf4_teardown(repetitions: int = 50, *, held: bool = False) -> dict[str, object]:
    written = SECOND_BYTES
    records: list[tuple[int, int, int, int]] = []
    with observing("pf4-held" if held else "pf4") as finish:
        client = Client("pf4")
        holder = Playback(client, "pf4-latency-holder") if held else None
        client.pump(3.0)
        for index in range(repetitions):
            playback = Playback(client, f"pf4-{index}")
            stream_index = playback.index()
            invoked = now_ns()
            playback.write(clip(index, written))
            client.pump(0.1)
            playback.close()
            listing = pactl("list", "short", "sink-inputs")
            listed = any(line.split("\t")[0] == str(stream_index) for line in listing.splitlines())
            confirmed = now_ns()
            records.append((index, invoked, confirmed, int(listed)))
            client.pump(2.5)
        if holder is not None:
            holder.close()
        client.close()
        recording = finish()
    after_bytes: list[int] = []
    after_lateness: list[int] = []
    observed_bytes: list[int] = []
    still_listed = 0
    unattributed = 0
    cursor = 0
    runs = recording.runs()
    for index, invoked, confirmed, was_listed in records:
        still_listed += was_listed
        source = clip(index, written)
        total = 0
        late = 0
        latest = 0
        for run in runs[cursor:]:
            if run.first_received_at < invoked:
                cursor += 1
                continue
            if len(run.data) < MIN_ATTRIBUTABLE_BYTES or source.find(run.data) < 0:
                break
            cursor += 1
            total += len(run.data)
            if run.last_received_at > confirmed:
                late += len(run.data)
                latest = max(latest, run.last_received_at - confirmed)
        observed_bytes.append(total)
        after_bytes.append(late)
        after_lateness.append(latest)
    unattributed = len(runs) - cursor
    return {
        "silent_stream_holding_sink_latency": held,
        "repetitions": repetitions,
        "written_bytes_per_stream": written,
        "played_before_disconnect_seconds": 0.1,
        "streams_still_listed_after_disconnect": still_listed,
        "observed_bytes_per_stream": summary(observed_bytes),
        "bytes_in_fragments_received_after_teardown_confirmed": summary(after_bytes),
        "latest_receipt_after_teardown_confirmed_ns": summary(after_lateness),
        "runs_not_attributed": unattributed,
        "observation_window_seconds": 2.5,
    }


def manifest() -> dict[str, object]:
    code = Path(__file__).parent
    return {
        "image": os.environ.get("SPIKE_IMAGE"),
        "commit": os.environ.get("SPIKE_COMMIT"),
        "pulseaudio": subprocess.run(
            ("pulseaudio", "--version"), capture_output=True, text=True, check=True
        ).stdout.strip(),
        "packages": subprocess.run(
            ("dpkg-query", "-W", "libpulse0", "pulseaudio"),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split("\n"),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "cpus": os.cpu_count(),
        "python": platform.python_version(),
        "default_pa_sha256": hashlib.sha256(
            Path("/etc/fakeyou/default.pa").read_bytes()
        ).hexdigest(),
        "daemon_conf_sha256": hashlib.sha256(
            Path("/etc/pulse/daemon.conf").read_bytes()
        ).hexdigest(),
        "code_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(code.glob("*.py"))
        },
    }


CHECKS: dict[str, Callable[[], dict[str, object]]] = {
    "SETUP-stream-start": setup_stream_start,
    "PF8": pf8_isolation,
    "PF5": pf5_latency,
    "PF6": pf6_stop_confirmation,
    "PF9": pf9_no_callback_in_section,
    "PF1": pf1_write_never_blocks,
    "PF2-socket": pf2_bytes_leave_only_when_pumped,
    "PF2-observed": pf2_observed_on_production_transport,
    "PF3": pf3_reply_not_from_stopped_daemon,
    "PF7": pf7_observer_attribution,
    "PF4": pf4_teardown,
    "PF4-held": lambda: pf4_teardown(held=True),
    "DIAG-late-release": diag_late_release,
}


def main() -> None:
    selected = sys.argv[1:] or list(CHECKS)
    target = OUT / "preflight.json"
    results: dict[str, object] = (
        json.loads(target.read_text()) if target.exists() else {"manifest": manifest()}
    )
    for name in selected:
        started = time.monotonic()
        try:
            outcome = CHECKS[name]()
        except (PulseError, OSError, RuntimeError, subprocess.SubprocessError) as error:
            outcome = {"established": False, "error": f"{type(error).__name__}: {error}"}
        outcome["seconds"] = round(time.monotonic() - started, 1)
        results[name] = outcome
        target.write_text(json.dumps(results, indent=2))
        sys.stdout.write(f"{name} {json.dumps(outcome)}\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
