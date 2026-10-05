(() => {
  if (window.__fakeyouAttributionProbe) return;
  const SAMPLE_MS = 50;
  const ROSTER_MS = 250;
  const LEVELS_MS = 250;
  const STATS_TIMEOUT_MS = 5000;
  const MAX_BUFFERED = 20000;
  const peers = [];
  const samples = [];
  const roster = [];
  const levels = [];
  const trackIds = new Map();
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
      if (report instanceof Map) return Array.from(report.values());
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
          if (!receiver || typeof receiver.getStats !== "function") continue;
          let report = null;
          try {
            report = await statsWithTimeout(receiver, STATS_TIMEOUT_MS);
          } catch {
            continue;
          }
          for (const entry of statEntries(report)) {
            try {
              if (!entry || entry.type !== "inbound-rtp") continue;
              if (entry.kind && entry.kind !== "audio") continue;
              const level = Number(entry.audioLevel);
              const trackId = String(entry.trackIdentifier || "");
              if (!Number.isFinite(level) || !trackId) continue;
              levels.push({
                page_ms: stamp,
                source: trackSource(trackId),
                audioLevel: level,
              });
            } catch {
              continue;
            }
          }
        }
      }
      trim(levels);
    } finally {
      levelsRunning = false;
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
      }));
    } catch {
      mapped = [];
    }
    roster.push({ page_ms: stamp, tiles: mapped });
    trim(roster);
  }

  if (OriginalPeerConnection) {
    function WrappedPeerConnection(...args) {
      const peer = new OriginalPeerConnection(...args);
      try {
        peers.push(peer);
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

  setInterval(guarded(sampleSources), SAMPLE_MS);
  setInterval(guarded(sampleRoster), ROSTER_MS);
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
          peer_connections: peers.length,
          receiver_count: seenReceivers,
        };
      } catch {
        return { samples: 0, roster: 0, levels: 0, peer_connections: 0, receiver_count: 0 };
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
          roster: roster.splice(0, roster.length),
        };
      } catch {
        return {
          page_ms: pageNow(),
          peer_connections: 0,
          receiver_count: 0,
          samples: [],
          levels: [],
          roster: [],
        };
      }
    },
  };
})();
