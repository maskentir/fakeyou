# Reference lessons

This note records reusable boundaries found during a read-only review of the sibling repositories. The current foundation is Python plus a native Playwright MCP/CloakBrowser path. The Vexa items below are future Node module seams unless they are deliberately vendored or published later.

Evidence was read at Vexa `dba990b413bd0f888b02d46a24f802db492addbb`, Attendee `c59b71b821f4556a02a810352e20baa31705e706`, and Amazon LMA `a23420adea9239ca874418856f4b7e69b96f24ec`. Those local checkout commits are the basis for the file and line references below.

## Recommended boundary

Use a meeting adapter that owns browser launch and returns a Playwright-compatible page. Keep meeting lifecycle, audio capture, transcription, memory, and graph orchestration behind separate ports. LangGraph/LangChain can coordinate those ports after a transcript event arrives; browser and media code should remain deterministic infrastructure.

Vexa's bot expresses this shape directly: `JoinDriver`, `Pipeline`, lifecycle, transcript, acts, and recording ports are injected into a pure orchestrator ([`ports.ts`](https://github.com/Vexa-ai/vexa/blob/dba990b413bd0f888b02d46a24f802db492addbb/core/meetings/services/bot/src/ports.ts), lines 1-15 and 63-104). Its join adapter passes a browser page to `@vexa/join` and maps admission outcomes to lifecycle states ([`join-driver.ts`](https://github.com/Vexa-ai/vexa/blob/dba990b413bd0f888b02d46a24f802db492addbb/core/meetings/services/bot/src/join-driver.ts), lines 1-18 and 33-50).

The current project can follow the same shape without importing Vexa: Python owns the orchestration and persistence, while native Playwright MCP drives CloakBrowser. A later Node worker can implement the same adapter contract if Vexa capture modules are adopted.

## Google Meet join

Vexa's `@vexa/join` is an isolated Google Meet join layer. It accepts a Playwright `Page`, supports guest or authenticated mode, and handles name entry, ask-to-join, admission, leave, and removal monitoring ([`join/src/index.ts`](https://github.com/Vexa-ai/vexa/blob/dba990b413bd0f888b02d46a24f802db492addbb/core/meetings/modules/join/src/index.ts), lines 1-10 and 43-59; [`join/src/googlemeet/README.md`](https://github.com/Vexa-ai/vexa/blob/dba990b413bd0f888b02d46a24f802db492addbb/core/meetings/modules/join/src/googlemeet/README.md), lines 1-7).

Google Meet interaction has two separate concerns. Vexa defaults to humanized X11 input for Meet because its implementation observed that browser-synthetic input can be rejected; keep this as a hypothesis to verify with CloakBrowser rather than treating it as a universal Cloak limitation ([`join/src/googlemeet/join.ts`](https://github.com/Vexa-ai/vexa/blob/dba990b413bd0f888b02d46a24f802db492addbb/core/meetings/modules/join/src/googlemeet/join.ts), lines 44-50). CloakBrowser claims a standard Playwright API and source-level fingerprint changes, so it is a candidate browser implementation, not a replacement for the join state machine ([CloakBrowser README](https://github.com/CloakHQ/CloakBrowser#api)).

The Amazon LMA navigation helper is a useful operational pattern: wait for `domcontentloaded`, allow a 60-second initial navigation budget, and accept a timeout when the settled URL already reached the expected meeting host ([`meeting-navigation.ts`](https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/blob/a23420adea9239ca874418856f4b7e69b96f24ec/lma-virtual-participant-stack/backend/src/meeting-navigation.ts), lines 42-89). This avoids classifying a loaded SPA as a failed join because a telemetry subresource did not settle.

## Inbound audio and speaker attribution

Vexa's Meet capture is the strongest reusable future reference. It finds rendered media elements carrying audio tracks, attaches an `AudioWorklet` per stream, emits 16 kHz PCM, rescans for late joiners, and silence-gates frames ([`gmeet-capture.ts`](https://github.com/Vexa-ai/vexa/blob/dba990b413bd0f888b02d46a24f802db492addbb/core/meetings/modules/gmeet-capture/src/gmeet-capture.ts), lines 9-15 and 52-140). `gmeet-capture-v1` stamps a name onto each chunk only when exactly one tile is visibly active; overlap remains unknown ([`gmeet-capture-v1.ts`](https://github.com/Vexa-ai/vexa/blob/dba990b413bd0f888b02d46a24f802db492addbb/core/meetings/modules/gmeet-capture/src/gmeet-capture-v1.ts), lines 4-18 and 45-76).

The attribution code is deliberately defensive. Meet's visible audio elements do not provide a stable participant mapping in all cases, so Vexa correlates channel energy with active-tile glow and enforces a confidence floor and one-to-one assignment ([`gmeet-channel-binder.ts`](https://github.com/Vexa-ai/vexa/blob/dba990b413bd0f888b02d46a24f802db492addbb/core/meetings/modules/gmeet-capture/src/gmeet-channel-binder.ts), lines 2-20 and 43-117). Its speaker reader suppresses the local tile and avoids automatic learning of obfuscated CSS classes after an earlier heuristic collapsed all channels onto one name ([`gmeet-speakers.ts`](https://github.com/Vexa-ai/vexa/blob/dba990b413bd0f888b02d46a24f802db492addbb/core/meetings/modules/gmeet-capture/src/gmeet-speakers.ts), lines 10-28 and 126-151). These are failure modes to preserve in future integration tests.

Vexa's worklet has a specific extension-only constraint: MV3 blocks the blob URL fallback, so an extension host must provide a web-accessible worklet module; ordinary in-page use can use the blob fallback ([`pcm-capture.ts`](https://github.com/Vexa-ai/vexa/blob/dba990b413bd0f888b02d46a24f802db492addbb/core/meetings/modules/gmeet-capture/src/pcm-capture.ts), lines 21-30 and 51-73). The current native browser path does not need the extension packaging branch.

Attendee confirms the useful media contracts: mixed or per-participant mono 16-bit PCM over WebSocket, participant UUIDs, and join/leave plus speech start/stop events ([`realtime_audio.md`](https://github.com/attendee-labs/attendee/blob/c59b71b821f4556a02a810352e20baa31705e706/docs/realtime_audio.md), lines 1-75; [`participant_events.md`](https://github.com/attendee-labs/attendee/blob/c59b71b821f4556a02a810352e20baa31705e706/docs/participant_events.md), lines 1-24). Its per-participant video contract is JPEG frames with `webcam` or `screenshare` source labels ([`realtime_video.md`](https://github.com/attendee-labs/attendee/blob/c59b71b821f4556a02a810352e20baa31705e706/docs/realtime_video.md), lines 1-60). These are interface references only for now.

## Outbound audio and video

Attendee's realtime audio output contract is a useful future seam: send base64 mono PCM with an explicit sample rate to a bot output channel ([`realtime_audio.md`](https://github.com/attendee-labs/attendee/blob/c59b71b821f4556a02a810352e20baa31705e706/docs/realtime_audio.md), lines 1-30). Vexa's current implementation routes synthesized PCM through PulseAudio `tts_sink` into a virtual microphone and mutes the source outside playback ([`tts-playback.ts`](https://github.com/Vexa-ai/vexa/blob/dba990b413bd0f888b02d46a24f802db492addbb/core/meetings/services/bot/src/tts-playback.ts), lines 4-16 and 22-96).

Muse Voice Transcribe is an STT service in this project scope. Keep outbound speech synthesis as a separate future provider seam; do not infer TTS support from the transcription integration. Native Pipecat `MetaSTTService` is the relevant STT integration direction identified for the foundation.

Amazon LMA also shows why recording and transcription must share one audio source. Its transcription path caps PCM frames before streaming and carries a cumulative timestamp offset across reconnects; these patterns prevent oversized-frame failures and overlapping timestamps (see [`scribe.ts`](https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/blob/a23420adea9239ca874418856f4b7e69b96f24ec/lma-virtual-participant-stack/backend/src/scribe.ts), around lines 49-121 and 178-237).

## Lifecycle and observability

Vexa classifies join failures at the browser source using typed outcome, raw platform detail, lobby timing, and attribution (`host_action`, `system_fault`, `user_action`, and so on), then carries that evidence on the terminal lifecycle event ([`join-evidence.ts`](https://github.com/Vexa-ai/vexa/blob/dba990b413bd0f888b02d46a24f802db492addbb/core/meetings/services/bot/src/join-evidence.ts), lines 1-27 and 43-83). Preserve the principle: a denial, lobby timeout, browser navigation failure, and user stop must remain distinct.

Attendee exposes a comparable event model through `bot.state_change`, `transcript.update`, participant join/leave, and participant speech webhooks ([`webhooks.md`](https://github.com/attendee-labs/attendee/blob/c59b71b821f4556a02a810352e20baa31705e706/docs/webhooks.md), lines 1-55 and 112-208). Its automatic leave utilities also show useful bounded policies for silence, solo meetings, and maximum uptime ([`automatic_leave_utils.py`](https://github.com/attendee-labs/attendee/blob/c59b71b821f4556a02a810352e20baa31705e706/bots/automatic_leave_utils.py), lines 1-50).

## Packaging and licenses

- Vexa is Apache-2.0 at the repository root. Its `@vexa/join` and `@vexa/remote-browser` package manifests are workspace packages at version `0.1.0`; the repository root is private. `npm view @vexa/join` and `npm view @vexa/remote-browser` both returned registry 404 on 2026-09-27. Treat them as source/workspace modules, not installable npm dependencies. If reused, vendor or extract a reviewed boundary with attribution and Apache notices.
- Attendee is Elastic License 2.0 ([`LICENSE`](https://github.com/attendee-labs/attendee/blob/c59b71b821f4556a02a810352e20baa31705e706/LICENSE)). Its hosted-service restriction matters if this project exposes a meeting-bot service, so copy behavior and contracts only unless the license is explicitly accepted.
- Amazon LMA's README says MIT-0, while its checked-in `LICENSE` is generic MIT text with a placeholder copyright holder. Resolve that inconsistency before redistributing copied source. Use it as a design reference until clarified.
- CloakBrowser's wrapper repository declares MIT ([`LICENSE`](https://raw.githubusercontent.com/CloakHQ/CloakBrowser/main/LICENSE)), but the official site says its downloadable binary cannot be redistributed, repackaged, or embedded in a third-party hosted service without an OEM/SaaS license ([licensing FAQ](https://cloakbrowser.dev/)). Download it through its official channel or obtain the required commercial terms before bundling it.

## Official platform constraint

Google Meet Media API is currently Developer Preview. It requires the Cloud project, OAuth principal, and all conference participants to be enrolled, uses restricted media scopes, and requires consent in the meeting ([official overview](https://developers.google.com/workspace/meet/media-api/guides/overview), [official setup and scopes](https://developers.google.com/workspace/meet/media-api/guides/get-started)). It is a future alternate backend for eligible meetings, not a foundation for arbitrary guest links.

## Current versus future reuse

Current foundation: Python agent/checkpoint composition, native Playwright MCP driving CloakBrowser, Muse Voice STT through the Pipecat adapter, DeepEval/pytest evaluation scaffolding, and Graphiti memory tools. Live media capture and captured transcript fixtures remain future work.

Future reusable Node boundaries: Vexa `@vexa/join` for Google Meet admission, Vexa `gmeet-capture` for per-stream PCM and attribution, Vexa lifecycle evidence vocabulary, and Vexa PulseAudio routing for outbound speech once the media transport is selected. OpenAI TTS is the current factory default; outbound transport is still missing. None of these Vexa modules are integrated into the current Python foundation; the registry checks recorded above apply to the audited packages.

## Live block mapping (2026-09-27)

Vexa documents our exact block screen verbatim: aggressive launch flags are "detectable by Google's bot layer and trigger 'You can't join this video call' on datacenter egress" (`remote-browser/src/args.ts`), and reCAPTCHA Enterprise scores every join invisibly in the background (`join/src/googlemeet/admission.ts`). A visible checkbox-sized `iframe[src*="recaptcha"]` means a live challenge; mere frame-URL presence does not.

Vexa's anti-block recipe, in one place: headed Chromium under Xvfb, `playwright-extra` stealth plugin plus an init script that spoofs WebGL vendor/renderer, defines `navigator.deviceMemory`, and stubs `chrome.runtime` (`remote-browser/src/browser.ts`), minimal clean flags including `--disable-blink-features=AutomationControlled` with no `--disable-web-security`-class flags (`join/src/browser-args.ts`), pinned UI locale via `--lang`/`--accept-lang`/context locale plus `?hl=` on the Meet URL, fake media stream flags, and OS-level XTEST humanized input as the default for Google Meet because "browser-synthetic input (Playwright/CDP clicks have isTrusted=false and no real pointer movement)" is blocked (`join/src/googlemeet/join.ts`).

Measured against our stack the same day: CloakBrowser headless masks `navigator.webdriver`, ships 5 plugins, and shows a real GPU on Mac, but leaks `deviceMemory === undefined` and missing `chrome.runtime` — the two tells Vexa explicitly patches. CloakBrowser's own flag set carries no `--disable-blink-features=AutomationControlled` and no fake-media flags; its `human/` input helpers drive CDP-level motion, not OS-level input, and the MCP tool path does not use them at all.

Vexa separates explicit host-denial copy ("denied your request", "weren't allowed to join") from bot-block pages, and checks rejection indicators before waiting-room indicators because lobby text lingers in the DOM after denial. Our adapter follows the same check order; its `denied` signal covers both "can't join this call" and the observed live "can't join this video call".

Amazon LMA has no Google Meet guest-join path (only a passing mention in its virtual-participant profile); it stays a timestamp/reconnect reference, not an admission reference.

## Attendee block handling (behavior only, Elastic-2.0)

Attendee treats "You can't join this video call" as a retryable platform block, explicitly distinct from host denial ("Someone in/on the call denied/has denied your request", "No one responded/has responded", "You left the meeting"). Its retry loop tolerates repeated expected failures, then escalates to a fresh pod once blocking repeats. When a Google login capability exists but is unused, a repeated block converts to login-required and the join is retried authenticated — login is documented in-code as what gets the bot unblocked.

Its baseline hygiene matches Vexa's family: headed browser, `AutomationControlled` disabled with the automation switch excluded, fake media flags, fixed window size, and a switchable humanized mode (recorded mouse trajectories, OS-level input, paced typing) next to a plain synthetic mode. It additionally names meeting-not-found codes and a "waiting for the host" state with its own timeout.

Design input for this project: split the conflated denied outcome into explicit host/nobody-responded denial (terminal) versus platform block (retryable with fresh state, then login escalation). Our six live attempts already used a fresh profile every run and were still blocked, so fresh state alone does not unblock this meeting — the manual signed-out check and an authenticated-join path are the two remaining discriminators.

## Vexa verdicts versus Attendee retries

Vexa classifies rejection into `host_denial` (explicit copy, wins over any captcha on the page), `error_page` (ambiguous affordance without a live challenge), and `captcha_unsolved` (visible challenge plus expired 120s solve grace) — then maps all three to one PERMANENT denial with no re-knock (`awaiting_admission_rejected`). Ambiguous states escalate to a human over VNC/CDP with a +5 minute intervention extension instead of being retried. Its dedicated `blocked` callback exists but is deliberately not emitted: the authors could not reproduce a block without false positives and left it unwired pending a datacenter-egress repro.

So the two references disagree on our exact situation: Attendee retries the block screen with fresh state and escalates to login; Vexa terminates it and summons a human. They agree on the rest: headed browser, clean flags, humanized input baseline, explicit-denial copy separated from generic error pages, and rejection checked before waiting/admission state. Note Vexa's own rejection list contains "can't join this call" but not the live "can't join this video call" variant — the same gap our adapter already fixed with a shorter substring.

Vexa's admission-detection guards are the deeper borrow for later: a waiting-room negative guard (lobby controls read as false admission positives), consent-gate suppression, toolbar wake via synthetic pointer move before visibility probes (Meet auto-hides the toolbar), and real-participant-tile counting that excludes the effects-panel phantom tile present in both lobby and call.

## LMA profile maturity (CloakBrowser-based)

LMA's virtual participant also runs on CloakBrowser, headed with `humanize: true`, and fights bot scoring through profile maturity rather than input tricks: a stable `--fingerprint` seed derived per user (a returning device instead of a never-before-seen one per launch), a persistent reused profile directory with stale-lock cleanup, warmup navigation over ordinary and meeting-platform sites on fresh profiles so the first join looks like a returning visitor, seeded bookmarks and search prefs so the profile never looks brand-new, and pre-launch `Preferences` patches allowing third-party cookies for meeting domains (including `meet.google.com`, `google.com`, `googleusercontent.com`) plus suppressed external-protocol choosers. It additionally strips Playwright's default `--mute-audio` flag and sizes screen/window coherently.

Adopted here: stable per-twin fingerprint seed (overriding CloakBrowser's random default, which its arg merge allows), the Google third-party-cookie pre-patch, persistent per-twin profiles under `.runtime/profiles/` with stale-lock cleanup, and fresh-profile warmup navigation. Seeded bookmarks/defaults remain unadopted as low-value cosmetics. The `--mute-audio` strip is recorded for the two-way audio phase.

## Direct drive beats MCP tools (proven live, 2026-09-28)

Eleven MCP-driven join attempts all ended in `CreateMeetingDevice` 403 (`PERMISSION_DENIED`, "The caller does not have permission"); the clicks were mechanically valid but the server rejected the device registration. The same flow driven through CloakBrowser's own humanized page API (`humanize=True`, direct locators) was admitted 3 of 4 attempts, then completed a full CLI lifecycle (admit → monitor → verified leave). Product consequence: meeting actions go through the humanized direct page; the MCP server stays as the observation/agent-tool layer, not the action layer.

CloakBrowser humanize constraints learned the hard way: `get_by_role`, chained locators, and frame locators are unsupported by its isolated-world resolver — only CSS, `text=`, `get_by_test_id`, `get_by_placeholder`, `get_by_alt_text`, `get_by_title`, `get_by_text`, `get_by_label`, plus trailing `.first`/`.nth()`/`.last`. The one direct-drive failure lacked a wait-for-visible before clicking; the implementation always waits for visibility first.

## Two-way audio topologies (proven patterns, 2026-09-28)

LMA's virtual participant runs full-duplex voice agents on a three-sink PulseAudio topology inside its Linux container: agent PCM plays into null sink `agent_output`; `module-remap-source` exposes `agent_output.monitor` as virtual mic `agent_mic`, set as the default source Chromium uses; meeting audio lands on null sink `meeting_audio` (default sink) and is captured from `meeting_audio.monitor`; a third sink mixes both for transcription via `module-loopback`. All sinks are pinned to 16 kHz mono s16le to match the pipeline and avoid resampling; the entrypoint checks existing sinks before loading modules (idempotent across retries); a watchdog logs device format changes. LMA explicitly forbids running this on a laptop — dev happens on Linux mirroring the production task. Adopted here as the Tahap 2 direction (see the media decision in `meeting-runtime-plan.md`).

Attendee's web bots instead inject outbound speech in-page: PCM chunks cross into the page and queue as WebAudio buffers feeding a gain node wired to a virtual mic track via a getUserMedia hook, with a virtual webcam path alongside. No OS audio device is needed, but every chunk crosses the automation bridge. Kept here as the macOS-native fallback, not the primary path.

Vexa's recording pipeline splits by platform: GMeet/Teams capture via browser-injected MediaRecorder chunks uploaded to the server; Zoom Web captures via a `parecord` subprocess from a PulseAudio sink monitor. Recording-grade, not real-time agent PCM — a reference for capture, not for the voice loop.

Humanized clicks need fresh-resolution retries (proven live, 2026-09-28)

A humanized click that meets a transient cover fails the entire call instead of recovering: two runs exhausted the full 30-second actionability wait on the name field ("covered by DIV") while the page was provably clear 2-3 seconds after load, and a fresh click call seconds later always landed in under a second. Fix: every lobby and leave click goes through a short-timeout, fresh-locator retry helper (3 attempts). Single-shot clicks are not trusted on the humanized path.

Identity-level join rate limiting (proven live, 2026-09-28)

After about 5 joins plus 15 page loads within an hour, one twin identity (stable fingerprint + persistent profile) was fast-rejected on every knock (`blocked_by_platform`) while a fresh identity reached the waiting room normally in the same meeting with the host present. Cause is per-identity bot-score/velocity gating, not meeting policy. Test implication: space joins apart or rotate test identities; normal use (one join per meeting) is unaffected.

Architecture cross-review

The Meet media bridge, two-way media, and speaker policy remain future work. The implementation verifies checkpoint isolation and persistence, Muse STT and TTS construction, scoped Graphiti tools, and a DeepEval tool trajectory. A real CloakBrowser/MCP smoke test verifies DOM, click, and screenshot tools on a local page. Muse live transcription passed with a synthetic Indonesian audio fixture. Guest admission, captured meeting audio, TTS live, and a complete meeting lifecycle graph remain unverified. Approval behavior is not implemented in this foundation. See the [current status](project-status.md) for the implementation sequence.

The document's recommendation to retain transcript provenance is ahead of the current code: `remember_meeting_fact` stores a free-text episode with a source description and timestamp, but there is no transcript ingestion or final-segment persistence adapter yet. Add that adapter only when the media seam exists, and keep provisional STT output out of long-term Graphiti writes.
