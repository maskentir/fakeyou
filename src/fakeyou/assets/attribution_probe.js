(() => {
  if (window.__fakeyouAttributionProbe) return;
  const SAMPLE_MS = 50;
  const ROSTER_MS = 250;
  const MAX_BUFFERED = 20000;
  const peers = [];
  const samples = [];
  const roster = [];
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
      receivers.forEach((receiver, receiverIndex) => {
        let kind = "";
        try {
          kind = receiver && receiver.track ? String(receiver.track.kind || "") : "";
        } catch {
          kind = "";
        }
        if (kind !== "" && kind !== "audio") return;
        samples.push({
          page_ms: stamp,
          peer: index,
          receiver: receiverIndex,
          sources: readSources(receiver, "getContributingSources"),
          sync: readSources(receiver, "getSynchronizationSources"),
        });
      });
    }
    trim(samples);
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

  window.__fakeyouAttributionProbe = {
    pending() {
      try {
        return {
          samples: samples.length,
          roster: roster.length,
          peer_connections: peers.length,
        };
      } catch {
        return { samples: 0, roster: 0, peer_connections: 0 };
      }
    },
    drain() {
      try {
        return {
          page_ms: pageNow(),
          peer_connections: peers.length,
          samples: samples.splice(0, samples.length),
          roster: roster.splice(0, roster.length),
        };
      } catch {
        return { page_ms: pageNow(), peer_connections: 0, samples: [], roster: [] };
      }
    },
  };
})();
