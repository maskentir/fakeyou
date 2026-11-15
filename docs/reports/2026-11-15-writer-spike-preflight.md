# Writer spike — preflight and diagnostic report

Status: **DIAGNOSTIC EVIDENCE. FULL S1 TRIALS NOT RUN AND NOT AUTHORIZED. V6 NO-GO. #205 UNCHANGED.**

This reports the preflight of `2026-11-15-writer-spike-protocol.md` (revision 4)
and one added diagnostic. It is not acceptance evidence for any writer. The
preflight found that assumptions of the protocol are wrong, so the protocol
must be revised before any trial; this report does not revise it.

| Item | Value |
| --- | --- |
| Harness | `evals/writer_spike/` at commit `eba185d7c4c6ac93a897d57e8eb192bceb31059f` |
| Results | `2026-11-15-writer-spike-preflight.json`: every number below, the manifest, and SHA-256 of each raw file |
| Raw files | Observer recordings, relay journal, run log, source tarball: 34 MB, not committed, kept locally under `.runtime/writer-spike/eba185d7/` |
| Image | `fakeyou-meeting-worker:local`, `sha256:d06aa1b9…3932`; `default.pa` and `daemon.conf` byte-identical to the repository |
| Server and client | PulseAudio 17.0, `libpulse0` 17.0+dfsg1-2+b1, Debian 13, kernel 7.0.14-linuxkit, aarch64, 10 CPUs |
| Source read | Upstream `pulseaudio-17.0.tar.xz`, SHA-256 `053794d6…b87b5`. The six Debian patches of 17.0+dfsg1-2 touch none of the files cited here |
| Scope | Offline, audio container only. No meeting, participant, browser or provider request. US$0 |

All numbers come from one run of that commit in a fresh container. Two earlier
passes on earlier code were used only to find what to ask; they are kept
locally and are not cited.

Stream attributes in effect unless a row says otherwise: `agent_output`,
24 kHz mono s16le, target length 40 ms, no prebuffer, first write placed at the
read index, no automatic timing updates, shared-memory transport as negotiated
by default.

## Preflight status

`established`, `refuted` or `unproven`. An argument I wrote from the source and
that nobody has reviewed is `unproven`, with the argument stated.

| # | Assumption in protocol revision 4 | Status | Observation | Source basis |
| --- | --- | --- | --- | --- |
| PF1 | The write call returns without blocking | **established** for the call | 3,000 calls: normal, 1,000 with nothing pumped, 1,000 with the daemon confirmed stopped. Slowest 0.069 ms | `pulse/stream.c` `pa_stream_write_ext_free`: copy, queue, return |
| PF1 | A short buffer is refused or reported by the write | **refuted** | 998 of 1,000 writes issued beyond the writable size returned success, with the daemon running and with it stopped | Same function has no capacity check. `pa_stream_writable_size` returns `requested_bytes`, a counter kept in the client |
| PF2 | The PCM reference is published to the server only during a mainloop iteration | **unproven** | Production transport: 0 of 200 clips observed before the first pump after a 500 ms hold, 200 of 200 after. Socket-only transport through a relay: 0 of 200 repetitions with any client byte before the pump | `pulsecore/pstream.c`: `pa_pstream_send_memblock` queues and enables a deferred event; `do_write` is reached from `defer_callback`, `io_callback`, `srb_callback` and `pa_pstream_set_srbchannel`, all inside dispatch. Not instrumented at the shared ring, not reviewed |
| PF2 | The socket can be observed externally | **refuted** for production | After the handshake the relay saw no further traffic; the connection moves to a shared ring channel | `pulse/context.c` `handle_srbchannel_memblock` |
| PF2 | The pending query reflects unsent data | **refuted** | True after a write before pumping, and also true with only a reply outstanding | `pulse/context.c` `pa_context_is_pending` also counts awaited replies |
| PF3 | A timing reply cannot precede server processing of an earlier write | **unproven**; not refuted | 0 of 100 replies arrived while the daemon was stopped; 100 of 100 after resume | `pulsecore/protocol-native.c`: `pstream_memblock_callback` posts the data to the sink's message queue; `command_get_playback_latency` sends a synchronous message on the same queue before replying. Not reviewed |
| PF4a | After the server stops listing a stream, nothing more is read from its buffer | **unproven** | 0 of 50 streams still listed after disconnect | `pa_sink_input_unlink` sends `PA_SINK_MESSAGE_REMOVE_INPUT` synchronously; the render thread removes the input before the caller continues. Not reviewed |
| PF4b | PCM already read downstream is gone or bounded once the stream is removed | **unproven** | 1 s written, about 0.1 s played, stream removed: 0 bytes of the clip in fragments received after teardown was confirmed, in 50 of 50 and again in 50 of 50 with a second stream holding the sink latency. 2.5 s window | `pa_sink_render` posts the mixed chunk to the monitor in the same call; the null sink discards it; the per-stream render queue is freed with the stream. Not reviewed. Fifty repetitions do not make this a guarantee |
| PF5 | Sink and monitor latency can be read | **established** | Sink configured 2,000 ms when idle, 20 ms with a stream connected. Monitor configured 2,000 ms | `module-null-sink.c`: `BLOCK_USEC` 2 s; `sink_update_requested_latency_cb` |
| PF6 | A daemon stop can be confirmed | **established** | 200 of 200: all three threads reported stopped, slowest confirmation 3.3 ms; all resumed | Kernel thread state |
| PF7 | The observer attributes samples to clips without error | **established** for this configuration | 300 of 300 clips, 100 of them truncated to random lengths, attributed uniquely against a library of 100,000; 0 nonzero samples in 10 s of silence; 0 ambiguous, 0 missing | Monitor is bit-exact at unity volume and equal sample format |
| PF8 | Nothing else can feed, move or uncork the streams | **established** | Four modules, exactly those of `default.pa`; no client but the query itself; one source output, owned by the remap module of `default.pa` | — |
| PF9 | Calls made inside the commit section invoke no callback | **established** | 1,000 repetitions each of writable-size query, write and uncork request: 0 callbacks, 0 mainloop iterations | The three functions queue and return |

PF8 note: the first version of the check counted the remap module's own reader
as foreign and reported failure. The reader is part of the unmodified
configuration. The check was corrected before the cited run.

## Five findings

### 1. Relative write without a prebuffer produces no audio

| Stream configuration | Seek mode | Clips written | Observed whole | Observed truncated | Not observed | Underrun | Indices at end |
| --- | --- | --- | --- | --- | --- | --- | --- |
| No prebuffer | Relative to write index | 100 | **0** | 0 | 100 | Continuous; 0 underflow notices | read 825,262, write 96,000 |
| No prebuffer | Relative to read index | 100 | **100** | 0 | 0 | 100 underflow notices | read 820,076, write 765,596 |
| One-frame prebuffer (20 ms) | Relative to write index | 100 | 2 | **98** | 0 | 100 underflow, 100 started | read 96,000, write 96,000 |

Status: the Arm R baseline of protocol revision 4 is invalid. With no prebuffer
the read index keeps advancing through an underrun, and a relative write lands
behind it.

Source: `pulsecore/memblockq.c` `pa_memblockq_seek`; `protocol-native.c`
`sink_input_pop_cb` and `handle_input_underrun`.

The third row resembles the production writer's buffer settings (40 ms target,
20 ms prebuffer, relative writes). Here 98 of 100 clips lost their beginning.
The production writer uses a different client API and was not tested; this is a
lead to check, not a finding about production.

### 2. Writing at the read index plays, including when it should not

Normal operation: 100 of 100 whole, write call to observer receipt median
0.6 ms, slowest 7.1 ms.

Source: the seek is applied by the render thread when it admits the data, as
`write_index = read_index + offset` at that moment (`protocol-native.c`
`SINK_INPUT_MESSAGE_SEEK`; `memblockq.c`). The position is decided when the PCM
arrives, not when it was written. There is no notion of a deadline.

This is a candidate mechanism for liveness. It is not safety-approved, and the
diagnostic below shows why.

### 3. PF2 on the production transport: three boundaries

| Boundary | When it happens | Basis | Standing |
| --- | --- | --- | --- |
| PCM memory exposure | At the write call, inside the commit section. The PCM is copied into a memory pool that the daemon also maps | `pulse/context.c` creates a shared pool; `pa_stream_write_ext_free` copies into it | From source only; not instrumented |
| Descriptor publication | During a later mainloop iteration, when the queued reference is written to the shared ring | `pstream.c` as in PF2 above | From source, plus 0 of 200 observed before the pump. `unproven` as a guarantee |
| Server consumption eligibility | After the daemon's main thread reads the reference and the render thread admits it to the stream buffer | `protocol-native.c` `pstream_memblock_callback`, then `SINK_INPUT_MESSAGE_SEEK` or `POST_DATA` | From source |

Consequences:

- "No PCM leaves the worker before the first pump" is false as written: the
  bytes are in shared memory at the write call. What can be argued is that the
  server has no reference to them until the pump.
- The pump-based lower bound stays unused. Its premise is `unproven`.
- The socket-only result is evidence about a transport the product does not use.

### 4. The write call gives no back-pressure

The call is non-blocking: established. It is not a refusal mechanism: refuted.
It accepted 20 s of audio with nothing pumped and with the daemon stopped. The
only capacity signal is the writable-size query, which reads a client-side
counter updated by server requests, so it is as stale as the last iteration and
knows nothing of a stopped daemon.

The pending query cannot separate unsent data from an awaited reply. No rule
distinguishing `data_unsent` from `reply_pending` could be validated, so
`sent_at` and `send_incomplete` should be removed from the accounting rather
than reinterpreted.

### 5. Stream lifecycle changes the sink

With no stream the null sink is configured for 2 s and renders up to that far
ahead; a connected stream brings it to 20 ms. Persistent-stream trials and
stream-per-trial trials therefore run against different sink states and must
not be pooled. In this run the difference did not show in what was heard after
teardown (PF4 with and without a holding stream agree), and a new stream's
first clip was not delayed. That is one observation at 50 repetitions each.

## Diagnostic: release that arrives late

Added after the first pass, at diagnostic scale: 50 clips per cell, one stream,
no prebuffer. The write call stands in for the final evaluation; the allowance
is 250 ms from it. "Absolute" places the clip at a stream index 40 ms ahead of
a read index taken from a timing snapshot just before.

| Scenario | Read-index placement | Absolute index +40 ms |
| --- | --- | --- |
| Pumped at once | 50 of 50 whole; receipt median 0.8 ms | 50 of 50 whole; receipt median 41.6 ms |
| Pump held 500 ms | 50 of 50 whole; receipt 505.6 ms after the write call | **0 of 50 observed** |
| Daemon confirmed stopped before the write, pumped during the stop, resumed after 500 ms | 50 of 50 whole; **50 confirmed late** | 50 of 50 whole; **50 confirmed late** |

How each cell is classed, under the bounds of protocol revision 4:

- **Daemon stopped: confirmed late output.** A stopped daemon renders nothing,
  so no sample left before the resume instant, which was about 500 ms after the
  write call in every repetition. All 100 clips were heard in full. This is
  output after the allowance that the writer did not prevent, for both
  placements.
- **Pump held, read-index placement: unproven as late, by the instrument.** The
  observer receipt is an upper bound and the pump-based lower bound is not
  accepted, so these 50 cannot be *confirmed* late. They were received about
  505 ms after the write call, 5 to 7 ms after the pump.
- **Pump held, absolute placement: dropped by the server.** By the time the
  reference arrived the read index had passed the clip, and nothing was heard.

Against the hypotheses stated before the run:

| Cell | Hypothesis | Result |
| --- | --- | --- |
| Pumped at once, both | Normal playback | Confirmed |
| Pump late, read index | Risk of stale playback | Stale playback observed in 50 of 50; lateness unproven under accepted bounds |
| Pump late, absolute | May drop as the read index advances | Confirmed: 0 of 50 heard |
| Daemon stopped, read index | Risk of stale playback | Confirmed late, 50 of 50 |
| Daemon stopped, absolute | Stale playback, since the stream clock stops with the daemon | Confirmed late, 50 of 50 |

What this does and does not show:

- An absolute stream position is not a wall-clock deadline. It rejected PCM
  delayed by the client and did nothing about a stalled server, because the
  index it is measured against stops when the daemon stops.
- The 40 ms lead was not varied and should not be. A different lead changes how
  often the stall case fails, not whether it can.
- In neither placement did the client have a way to notice: the write
  succeeded and the writable-size counter was unchanged while the daemon was
  stopped.
- Fifty repetitions per cell on one host. The stall cells are confirmed by a
  deterministic bound, not by a rate.

## Disposition against the ruled table

| Evidence | Applies |
| --- | --- |
| Confirmed late output that violates V2 | **Yes**, for the asynchronous writer with read-index placement and with absolute placement, when the daemon stalls between the commit and the admission of the PCM |
| No confirmed violation, prevention not shown | The pump-held read-index cell |
| No violation and a complete prevention argument | None |

By that table both configurations are rejected for V6. The decision is the
reviewer's. No mechanism was found in PulseAudio 17.0 that refuses PCM by
wall-clock time at the stage that consumes it: the two placement rules found
are positions in stream time, and stream time is driven by the daemon.

Nothing here says a deadline check at the consuming stage is impossible in a
component built for it. That is a different architecture, not examined here.

## Assumptions of protocol revision 4 shown wrong

1. Arm R as specified ("running, empty, no prebuffer", relative write) produces audio. It produces none.
2. A short buffer is reported by the write. The write never refuses.
3. The client socket can be watched from outside. Not on the production transport.
4. The pending query means unsent data. It also means an awaited reply.
5. No PCM is outside the worker before the pump. It is in shared memory at the write call.
6. Persistent and per-trial streams measure one population. The sink is configured differently.

Carried forward, not applied: the ruled K8c clarification (a downstream bound
in bytes and one in time are checked separately, never added). It was removed
from revision 4 again and belongs to the next revision with its own status.

## Not done

- No full trial of any condition C0–C13.
- No revision of Arm R, PF2, readiness rules or stream lifecycle.
- No instrumentation of the shared ring or of daemon internals; PF2, PF3, PF4a and PF4b rest on source reading that has not been reviewed.
- The production `pa_simple` writer was not exercised.
- No CPU-contention, restart or cancellation scenario.
