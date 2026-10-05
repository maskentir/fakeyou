(() => {
  if (window.__fakeyouAttributionProbe) return;
  const SAMPLE_MS = 50;
  const FRAME_WINDOW_MS = 100;
  const ROSTER_MS = 250;
  const LEVELS_MS = 250;
  const STATS_TIMEOUT_MS = 5000;
  const MAX_BUFFERED = 20000;
  const peers = [];
  const samples = [];
  const roster = [];
  const levels = [];
  const trackIds = new Map();
  const statsDiag = new Map();
  let mediaCensus = null;
  let audioTopology = null;
  let audioContexts = 0;
  const audioSources = [];
  const audioTaps = [];
  const tapBuffer = new Float32Array(2048);
  let topologyRunning = false;
  let seenReceivers = 0;
  let levelsRunning = false;
  const OriginalPeerConnection =
    typeof window.RTCPeerConnection === "function"
      ? window.RTCPeerConnection
      : null;

  function pageNow() {
    try {
      return window.performance.timeOrigin + window.performance.now();
    } catch {
      return Date.now();
    }
  }

  function trim(list) {
    if (list.length > MAX_BUFFERED) list.splice(0, list.length - MAX_BUFFERED);
  }

  function readSources(receiver, method) {
    try {
      const reader = receiver[method];
      if (typeof reader !== "function") return [];
      const found = reader.call(receiver) || [];
      const out = [];
      for (const entry of found) {
        out.push({
          source: Number(entry.source) || 0,
          audioLevel: Number(entry.audioLevel) || 0,
          timestamp: Number(entry.timestamp) || 0,
        });
      }
      return out;
    } catch {
      return [];
    }
  }

  function sampleSources() {
    const stamp = pageNow();
    for (let index = 0; index < peers.length; index += 1) {
      let receivers = [];
      try {
        const got = peers[index].getReceivers
          ? peers[index].getReceivers()
          : [];
        receivers = Array.isArray(got) ? got : [];
      } catch {
        receivers = [];
      }
      let audioCount = 0;
      receivers.forEach((receiver, receiverIndex) => {
        const track = readTrack(receiver);
        if (track.kind !== "" && track.kind !== "audio") return;
        audioCount += 1;
        try {
          const transform = receiver.transform;
          const kind = transform && transform.constructor ? String(transform.constructor.name) : "none";
          if (!pipeline.receiver_transforms.includes(kind)) pipeline.receiver_transforms.push(kind);
        } catch {
          /* diagnostics only */
        }
        samples.push({
          page_ms: stamp,
          peer: index,
          receiver: receiverIndex,
          track,
          sources: readSources(receiver, "getContributingSources"),
          sync: readSources(receiver, "getSynchronizationSources"),
        });
      });
      if (audioCount > seenReceivers) seenReceivers = audioCount;
    }
    trim(samples);
  }

  function readTrack(receiver) {
    const track = { id: "", kind: "", muted: false, readyState: "" };
    try {
      const source = receiver ? receiver.track : null;
      if (!source) return track;
      track.id = String(source.id || "").slice(0, 64);
      track.kind = String(source.kind || "");
      track.muted = !!source.muted;
      track.readyState = String(source.readyState || "");
    } catch {
      /* keep defaults */
    }
    return track;
  }

  function trackSource(trackId) {
    if (!trackId) return -1;
    if (!trackIds.has(trackId)) trackIds.set(trackId, trackIds.size);
    return trackIds.get(trackId);
  }

  function statEntries(report) {
    try {
      if (report && typeof report.forEach === "function") {
        const entries = [];
        report.forEach((entry) => entries.push(entry));
        return entries;
      }
      if (report && typeof report === "object") return Object.values(report);
    } catch {
      /* fall through */
    }
    return [];
  }

  async function statsWithTimeout(receiver, ms) {
    let timer = null;
    try {
      return await Promise.race([
        receiver.getStats(),
        new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error("stats timeout")), ms);
        }),
      ]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  async function sampleLevels() {
    if (levelsRunning) return;
    levelsRunning = true;
    try {
      const stamp = pageNow();
      for (const peer of peers) {
        let receivers = [];
        try {
          const got = peer.getReceivers ? peer.getReceivers() : [];
          receivers = Array.isArray(got) ? got : [];
        } catch {
          receivers = [];
        }
        for (const receiver of receivers) {
          const trackKey = readTrack(receiver).id || "no-track";
          if (!receiver || typeof receiver.getStats !== "function") {
            statsDiag.set(trackKey, {
              track: trackKey,
              types: [],
              entries: 0,
              audio: false,
              error: "no-getstats",
            });
            continue;
          }
          let report = null;
          try {
            report = await statsWithTimeout(receiver, STATS_TIMEOUT_MS);
          } catch (error) {
            statsDiag.set(trackKey, {
              track: trackKey,
              types: [],
              entries: 0,
              audio: false,
              error: String((error && error.message) || error).slice(0, 80),
            });
            continue;
          }
          const listed = statEntries(report);
          const types = [];
          let audio = false;
          for (const entry of listed) {
            let kind = "";
            try {
              kind = entry && entry.type ? String(entry.type) : "";
              if (kind && !types.includes(kind)) types.push(kind);
              if (!entry || entry.type !== "inbound-rtp") continue;
              if (entry.kind && entry.kind !== "audio") continue;
              const level = Number(entry.audioLevel);
              const trackId = String(entry.trackIdentifier || "");
              if (!Number.isFinite(level) || !trackId) continue;
              audio = true;
              levels.push({
                page_ms: stamp,
                source: trackSource(trackId),
                audioLevel: level,
              });
            } catch {
              continue;
            }
          }
          statsDiag.set(trackKey, {
            track: trackKey,
            types: types.slice(0, 12),
            entries: listed.length,
            audio,
            error: null,
          });
        }
      }
      trim(levels);
    } finally {
      levelsRunning = false;
    }
  }

  function sampleMediaCensus() {
    let elements = [];
    try {
      elements = Array.from(document.querySelectorAll("audio, video")).slice(0, 16);
    } catch {
      elements = [];
    }
    mediaCensus = elements.map((el) => {
      const entry = {
        tag: "",
        hasSrcObject: false,
        audioTracks: 0,
        trackIds: [],
        paused: true,
        muted: true,
        participantId: null,
        tileName: null,
      };
      try {
        entry.tag = String(el.tagName || "").toLowerCase();
        const stream = el.srcObject;
        entry.hasSrcObject = stream instanceof MediaStream;
        if (entry.hasSrcObject) {
          const tracks = stream.getAudioTracks();
          entry.audioTracks = tracks.length;
          entry.trackIds = tracks.map((track) => String(track.id || "").slice(0, 16));
        }
        entry.paused = !!el.paused;
        entry.muted = !!el.muted;
        const tile = el.closest("[data-participant-id]");
        if (tile) {
          entry.participantId = tile.getAttribute("data-participant-id") || null;
          entry.tileName = tileName(tile) || null;
        }
      } catch {
        /* keep defaults */
      }
      return entry;
    });
  }

  function wrapAudioContext(name) {
    const Original = window[name];
    if (typeof Original !== "function" || Original.__fakeyouWrapped) return;
    function Wrapped(...args) {
      const ctx = new Original(...args);
      audioContexts += 1;
      try {
        const originalSource = ctx.createMediaStreamSource;
        if (typeof originalSource === "function") {
          ctx.createMediaStreamSource = function (stream) {
            const node = originalSource.call(this, stream);
            try {
              const tracks = stream ? stream.getAudioTracks() : [];
              const key = tapKey(stream, tracks);
              audioSources.push({
                tracks: tracks.map((track) => String(track.id || "").slice(0, 16)),
              });
              for (const track of tracks) {
                const kind = track && track.constructor ? String(track.constructor.name) : "";
                if (!pipeline.source_track_types.includes(kind)) {
                  pipeline.source_track_types.push(kind);
                }
              }
              if (audioSources.length > 16) audioSources.splice(0, 1);
              const analyser = ctx.createAnalyser();
              analyser.fftSize = 2048;
              node.connect(analyser);
              audioTaps.push({ analyser, key });
              if (audioTaps.length > 16) audioTaps.splice(0, 1);
            } catch {
              /* diagnostics only */
            }
            return node;
          };
        }
      } catch {
        /* diagnostics only */
      }
      return ctx;
    }
    Wrapped.prototype = Original.prototype;
    Object.setPrototypeOf(Wrapped, Original);
    Wrapped.__fakeyouWrapped = true;
    try {
      window[name] = Wrapped;
    } catch {
      /* page locked the constructor */
    }
  }

  function tapKey(stream, tracks) {
    try {
      if (tracks.length > 0 && tracks[0].id) return String(tracks[0].id);
      if (stream && stream.id) return String(stream.id);
    } catch {
      /* fall through */
    }
    return "tap-" + audioTaps.length;
  }

  function sampleTapEnergy() {
    const stamp = pageNow();
    for (const tap of audioTaps) {
      try {
        tap.analyser.getFloatTimeDomainData(tapBuffer);
        let sum = 0;
        for (let index = 0; index < tapBuffer.length; index += 1) {
          sum += tapBuffer[index] * tapBuffer[index];
        }
        levels.push({
          page_ms: stamp,
          source: trackSource(tap.key),
          audioLevel: Math.sqrt(sum / tapBuffer.length),
        });
      } catch {
        continue;
      }
    }
    trim(levels);
  }

  async function sampleAudioTopology() {
    if (topologyRunning) return;
    topologyRunning = true;
    try {
      let entries = 0;
      const types = [];
      for (const peer of peers) {
        if (!peer || typeof peer.getStats !== "function") continue;
        let report = null;
        try {
          report = await statsWithTimeout(peer, STATS_TIMEOUT_MS);
        } catch {
          continue;
        }
        for (const entry of statEntries(report)) {
          entries += 1;
          try {
            const kind = entry && entry.type ? String(entry.type) : "";
            if (kind && !types.includes(kind)) types.push(kind);
          } catch {
            continue;
          }
        }
      }
      audioTopology = {
        contexts: audioContexts,
        sources: audioSources.slice(-16),
        pc_stats_entries: entries,
        pc_stats_types: types.slice(0, 12),
      };
    } finally {
      topologyRunning = false;
    }
  }

  function tileName(tile) {
    try {
      const direct = tile.querySelector("span.notranslate");
      const text = (direct && direct.textContent ? direct.textContent : "").trim();
      if (text) return text.slice(0, 80);
      const marker = tile.querySelector("[data-self-name]");
      const selfName = marker
        ? String(marker.getAttribute("data-self-name") || "").trim()
        : "";
      return selfName.slice(0, 80);
    } catch {
      return "";
    }
  }

  function tileSelf(tile, selfId) {
    try {
      if (tile.hasAttribute("data-self-name")) return true;
      if (tile.querySelector("[data-self-name]")) return true;
      const id = tile.getAttribute("data-participant-id") || "";
      return selfId !== "" && id === selfId;
    } catch {
      return false;
    }
  }

  function glowCandidates(tile) {
    const found = { es310d: false, knownClass: false };
    try {
      const marker = tile.querySelector('[jscontroller="ES310d"]');
      found.es310d = !!marker && marker.offsetParent !== null;
    } catch {
      found.es310d = false;
    }
    try {
      const classes = ["speaking", "active-speaker", "speaker-active"];
      for (const name of classes) {
        if (tile.classList.contains(name) || tile.querySelector("." + name)) {
          found.knownClass = true;
          break;
        }
      }
    } catch {
      found.knownClass = false;
    }
    return found;
  }

  function tileAria(tile) {
    try {
      return String(tile.getAttribute("aria-label") || "").slice(0, 200);
    } catch {
      return "";
    }
  }

  function tileCls(tile) {
    try {
      const raw = tile.className && tile.className.baseVal !== undefined
        ? ""
        : String(tile.className || "");
      return raw.slice(0, 500);
    } catch {
      return "";
    }
  }

  function tileHtml(tile) {
    try {
      return String(tile.outerHTML || "").slice(0, 800);
    } catch {
      return "";
    }
  }

  function tileMuted(tile) {
    try {
      const muted = tile.querySelector("[data-is-muted]");
      if (muted) return muted.getAttribute("data-is-muted") === "true";
      const label = String(tile.getAttribute("aria-label") || "").toLowerCase();
      return label.indexOf("muted") >= 0;
    } catch {
      return false;
    }
  }

  function sampleRoster() {
    const stamp = pageNow();
    let tiles = [];
    try {
      tiles = Array.from(
        document.querySelectorAll("[data-participant-id]")
      ).slice(0, 32);
    } catch {
      tiles = [];
    }
    let selfId = "";
    try {
      const marker = document.querySelector("[data-self-name]");
      const tile = marker ? marker.closest("[data-participant-id]") : null;
      selfId = tile ? tile.getAttribute("data-participant-id") || "" : "";
    } catch {
      selfId = "";
    }
    let mapped = [];
    try {
      mapped = tiles.map((tile) => ({
        id: tile.getAttribute("data-participant-id") || "",
        name: tileName(tile),
        self: tileSelf(tile, selfId),
        speaking: glowCandidates(tile),
        muted: tileMuted(tile),
        aria: tileAria(tile),
        cls: tileCls(tile),
        html: tileHtml(tile),
      }));
    } catch {
      mapped = [];
    }
    roster.push({ page_ms: stamp, tiles: mapped });
    trim(roster);
  }

  const pipeline = {
    insertable_configs: 0,
    encoded_streams: 0,
    script_transforms: 0,
    track_generators: 0,
    track_processors: 0,
    audio_decoders: 0,
    worklet_nodes: 0,
    receiver_transforms: [],
    source_track_types: [],
  };

  function countConstructor(name, key) {
    const Original = window[name];
    if (typeof Original !== "function") return;
    function Counted(...args) {
      pipeline[key] += 1;
      return new Original(...args);
    }
    Counted.prototype = Original.prototype;
    Object.setPrototypeOf(Counted, Original);
    try {
      window[name] = Counted;
    } catch {
      /* page locked the constructor */
    }
  }

  const frameWindows = new Map();
  const frames = [];
  let encodedReceivers = 0;

  function noteFrame(receiverIndex, trackId, frame) {
    const meta = frame.getMetadata();
    const bucket = Math.floor(pageNow() / FRAME_WINDOW_MS) * FRAME_WINDOW_MS;
    const key = receiverIndex + ":" + bucket;
    let entry = frameWindows.get(key);
    if (!entry) {
      entry = {
        page_ms: bucket,
        receiver: receiverIndex,
        track: trackId,
        ssrc: [],
        csrc: [],
        frames: 0,
        level: 0,
      };
      frameWindows.set(key, entry);
      frames.push(entry);
      trim(frames);
      if (frameWindows.size > MAX_BUFFERED) frameWindows.clear();
    }
    entry.frames += 1;
    const level = Number(meta.audioLevel);
    if (Number.isFinite(level) && level > entry.level) entry.level = level;
    const ssrc = Number(meta.synchronizationSource);
    if (Number.isFinite(ssrc) && !entry.ssrc.includes(ssrc)) entry.ssrc.push(ssrc);
    for (const source of meta.contributingSources || []) {
      const csrc = Number(source);
      if (Number.isFinite(csrc) && !entry.csrc.includes(csrc)) entry.csrc.push(csrc);
    }
  }

  function drainFrames() {
    frameWindows.clear();
    return frames.splice(0, frames.length);
  }

  function countEncodedStreams() {
    try {
      const proto = window.RTCRtpReceiver && window.RTCRtpReceiver.prototype;
      if (!proto || typeof proto.createEncodedStreams !== "function") return;
      const original = proto.createEncodedStreams;
      proto.createEncodedStreams = function (...args) {
        pipeline.encoded_streams += 1;
        const streams = original.apply(this, args);
        try {
          if (!this.track || this.track.kind !== "audio") return streams;
          const receiverIndex = encodedReceivers;
          const trackId = String(this.track.id || "").slice(0, 16);
          encodedReceivers += 1;
          const tap = new TransformStream({
            transform(frame, controller) {
              try {
                noteFrame(receiverIndex, trackId, frame);
              } catch {
                /* metadata is optional */
              }
              controller.enqueue(frame);
            },
          });
          return { readable: streams.readable.pipeThrough(tap), writable: streams.writable };
        } catch {
          return streams;
        }
      };
    } catch {
      /* diagnostics only */
    }
  }

  if (OriginalPeerConnection) {
    function WrappedPeerConnection(...args) {
      const peer = new OriginalPeerConnection(...args);
      try {
        peers.push(peer);
        if (args[0] && args[0].encodedInsertableStreams) pipeline.insertable_configs += 1;
      } catch {
        /* never break page construction */
      }
      return peer;
    }
    WrappedPeerConnection.prototype = OriginalPeerConnection.prototype;
    Object.setPrototypeOf(WrappedPeerConnection, OriginalPeerConnection);
    try {
      window.RTCPeerConnection = WrappedPeerConnection;
    } catch {
      /* page locked the constructor; sampling continues without capture */
    }
  }

  function guarded(fn) {
    return () => {
      try {
        fn();
      } catch {
        /* sampler must never break the page */
      }
    };
  }

  countConstructor("RTCRtpScriptTransform", "script_transforms");
  countConstructor("MediaStreamTrackGenerator", "track_generators");
  countConstructor("MediaStreamTrackProcessor", "track_processors");
  countConstructor("AudioDecoder", "audio_decoders");
  countConstructor("AudioWorkletNode", "worklet_nodes");
  countEncodedStreams();
  wrapAudioContext("AudioContext");
  wrapAudioContext("webkitAudioContext");
  wrapAudioContext("OfflineAudioContext");

  setInterval(guarded(sampleSources), SAMPLE_MS);
  setInterval(guarded(sampleRoster), ROSTER_MS);
  setInterval(guarded(sampleMediaCensus), ROSTER_MS);
  setInterval(
    guarded(() => {
      sampleAudioTopology().catch(() => {});
      sampleTapEnergy();
    }),
    ROSTER_MS
  );
  setInterval(guarded(() => {
    sampleLevels().catch(() => {});
  }), LEVELS_MS);

  window.__fakeyouAttributionProbe = {
    pending() {
      try {
        return {
          samples: samples.length,
          roster: roster.length,
          levels: levels.length,
          census: mediaCensus ? mediaCensus.length : 0,
          topology: audioTopology ? 1 : 0,
          peer_connections: peers.length,
          receiver_count: seenReceivers,
        };
      } catch {
        return {
          samples: 0,
          roster: 0,
          levels: 0,
          census: 0,
          topology: 0,
          peer_connections: 0,
          receiver_count: 0,
        };
      }
    },
    drain() {
      try {
        return {
          page_ms: pageNow(),
          peer_connections: peers.length,
          receiver_count: seenReceivers,
          samples: samples.splice(0, samples.length),
          levels: levels.splice(0, levels.length),
          stats_diag: Array.from(statsDiag.values()).slice(0, 16),
          pipeline,
          frames: drainFrames(),
          media_census: mediaCensus || [],
          audio_topology: audioTopology || {
            contexts: 0,
            sources: [],
            pc_stats_entries: 0,
            pc_stats_types: [],
          },
          roster: roster.splice(0, roster.length),
        };
      } catch {
        return {
          page_ms: pageNow(),
          peer_connections: 0,
          receiver_count: 0,
          samples: [],
          levels: [],
          stats_diag: [],
          media_census: [],
          audio_topology: {
            contexts: 0,
            sources: [],
            pc_stats_entries: 0,
            pc_stats_types: [],
          },
          roster: [],
        };
      }
    },
  };
})();
