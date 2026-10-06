(() => {
  if (window.__fakeyouStreamCapture) return;
  const TARGET_RATE = 16000;
  const WINDOW_MS = 50;
  const WINDOW_SAMPLES = (TARGET_RATE * WINDOW_MS) / 1000;
  const MAX_BUFFERED = 6000;
  const MAX_PARTICIPANTS = 32;
  const MAX_CARRY_SECONDS = 2;
  const TEST_FREQUENCIES = [440, 880];
  const TEST_PAIR_TIMEOUT_MS = 8000;
  const slots = new Map();
  const peers = [];
  const windows = [];
  const pendingSamples = new Map();
  const receiversBySlot = new Map();
  const resamplers = new Map();
  const readersBySlot = new Map();
  const testGains = [];
  const testPairs = [];
  const testReceivers = new Set();
  const keeps = [];
  let dropped = 0;
  let nextSlot = 0;
  window.__fakeyouStreamCaptureNodes = keeps;

  function pageNow() {
    try {
      return window.performance.timeOrigin + window.performance.now();
    } catch {
      return Date.now();
    }
  }

  function slotFor(trackId) {
    if (!slots.has(trackId)) {
      slots.set(trackId, nextSlot);
      nextSlot += 1;
    }
    return slots.get(trackId);
  }

  function readCsrcs(receiver) {
    const found = [];
    if (!receiver) return found;
    const methods = ["getContributingSources", "getSynchronizationSources"];
    for (const method of methods) {
      try {
        const reader = receiver[method];
        if (typeof reader !== "function") continue;
        const list = reader.call(receiver) || [];
        for (const entry of list) {
          const csrc = Number(entry && entry.source);
          if (Number.isFinite(csrc) && !found.includes(csrc)) found.push(csrc);
        }
      } catch {
        /* sources are optional */
      }
    }
    return found;
  }

  function pushSamples(slot, chunk) {
    const acc = pendingSamples.get(slot);
    if (!acc || !chunk || !chunk.length) return;
    for (let index = 0; index < chunk.length; index += 1) acc.push(chunk[index]);
    while (acc.length >= WINDOW_SAMPLES) {
      const frame = acc.splice(0, WINDOW_SAMPLES);
      let sum = 0;
      for (let index = 0; index < frame.length; index += 1) sum += frame[index] * frame[index];
      const level = Math.sqrt(sum / frame.length) / 32768;
      windows.push({
        slot,
        csrcs: readCsrcs(receiversBySlot.get(slot)),
        level,
        page_ms: pageNow(),
        pcm: frame,
      });
      if (windows.length > MAX_BUFFERED) {
        const overflow = windows.length - MAX_BUFFERED;
        windows.splice(0, overflow);
        dropped += overflow;
      }
    }
  }

  function pushFloat(slot, frames, rate) {
    if (!frames || !frames.length || !Number.isFinite(rate) || rate <= 0) return;
    let state = resamplers.get(slot);
    if (!state) {
      state = { carry: [], pos: 0 };
      resamplers.set(slot, state);
    }
    for (let index = 0; index < frames.length; index += 1) state.carry.push(frames[index]);
    const step = rate / TARGET_RATE;
    const out = [];
    while (Math.floor(state.pos) + 1 < state.carry.length) {
      const at = Math.floor(state.pos);
      const frac = state.pos - at;
      const mixed = state.carry[at] + (state.carry[at + 1] - state.carry[at]) * frac;
      const clamped = Math.max(-1, Math.min(1, mixed));
      out.push(clamped < 0 ? Math.round(clamped * 32768) : Math.round(clamped * 32767));
      state.pos += step;
    }
    const drop = Math.min(Math.floor(state.pos), state.carry.length - 1);
    if (drop > 0) {
      state.carry.splice(0, drop);
      state.pos -= drop;
    }
    if (state.carry.length > rate * MAX_CARRY_SECONDS) {
      state.carry.splice(0, state.carry.length - 1);
      state.pos = 0;
    }
    pushSamples(slot, out);
  }

  function handleAudioData(slot, data) {
    try {
      if (!data || !data.numberOfFrames) return;
      const rate = Number(data.sampleRate) || 48000;
      const bytes = data.allocationSize({ planeIndex: 0, format: "f32-planar" });
      const plane = new Float32Array(bytes / 4);
      data.copyTo(plane, { planeIndex: 0, format: "f32-planar" });
      pushFloat(slot, plane, rate);
    } catch {
      /* one bad frame must not stop capture */
    }
  }

  async function pumpSlot(slot, reader) {
    for (;;) {
      let packet = null;
      try {
        packet = await reader.read();
      } catch {
        return;
      }
      if (!packet || packet.done) {
        if (packet && packet.value) {
          try {
            packet.value.close();
          } catch {
            /* already closed */
          }
        }
        return;
      }
      try {
        handleAudioData(slot, packet.value);
      } finally {
        try {
          packet.value.close();
        } catch {
          /* already closed */
        }
      }
    }
  }

  function releaseSlot(slot, trackId) {
    try {
      const reader = readersBySlot.get(slot);
      if (reader) {
        readersBySlot.delete(slot);
        reader.cancel().catch(() => {});
      }
    } catch {
      /* reader already gone */
    }
    receiversBySlot.delete(slot);
    pendingSamples.delete(slot);
    resamplers.delete(slot);
    if (trackId) slots.delete(trackId);
  }

  function attachReceiver(receiver) {
    const track = receiver ? receiver.track : null;
    if (!track || track.kind !== "audio") return;
    const key = String(track.id || "");
    if (!key || slots.has(key)) return;
    const slot = slotFor(key);
    receiversBySlot.set(slot, receiver);
    pendingSamples.set(slot, []);
    try {
      const el = document.createElement("audio");
      el.muted = true;
      el.srcObject = new MediaStream([track]);
      const processor = new MediaStreamTrackProcessor({ track });
      const reader = processor.readable.getReader();
      readersBySlot.set(slot, reader);
      keeps.push({ el, processor, reader, receiver });
      const played = el.play();
      if (played && typeof played.catch === "function") played.catch(() => {});
      track.addEventListener("ended", () => releaseSlot(slot, key), { once: true });
      void pumpSlot(slot, reader);
    } catch {
      releaseSlot(slot, key);
    }
  }

  function onTrack(event) {
    try {
      const pc = event ? event.target || event.currentTarget : null;
      if (pc && testReceivers.has(pc)) return;
      if (event && event.receiver) {
        attachReceiver(event.receiver);
        return;
      }
      const track = event ? event.track : null;
      if (pc && track && typeof pc.getReceivers === "function") {
        for (const receiver of pc.getReceivers()) {
          if (receiver && receiver.track && receiver.track.id === track.id) {
            attachReceiver(receiver);
            return;
          }
        }
      }
    } catch {
      /* tapping must never break the page */
    }
  }

  function wrapPeerConnection() {
    const Original = window.RTCPeerConnection;
    if (typeof Original !== "function" || Original.__fakeyouCaptureWrapped) return;
    function Wrapped(...args) {
      const pc = new Original(...args);
      peers.push(pc);
      try {
        pc.addEventListener("track", onTrack);
      } catch {
        /* page locked the peer */
      }
      return pc;
    }
    Wrapped.prototype = Original.prototype;
    Object.setPrototypeOf(Wrapped, Original);
    Wrapped.__fakeyouCaptureWrapped = true;
    try {
      window.RTCPeerConnection = Wrapped;
    } catch {
      /* page locked the constructor */
    }
  }

  function encodeBase64(bytes) {
    let text = "";
    for (let index = 0; index < bytes.length; index += 0x8000) {
      text += String.fromCharCode.apply(null, bytes.subarray(index, index + 0x8000));
    }
    return btoa(text);
  }

  function participants() {
    const roster = [];
    try {
      const tiles = document.querySelectorAll("[data-participant-id]");
      for (const tile of tiles) {
        const id = String(tile.getAttribute("data-participant-id") || "");
        if (!id) continue;
        const label = tile.querySelector("span.notranslate");
        const name = label && label.textContent ? label.textContent.trim() : "";
        roster.push({ id: id.slice(0, 128), name: name.slice(0, 80) });
        if (roster.length >= MAX_PARTICIPANTS) break;
      }
    } catch {
      /* roster unavailable this round */
    }
    return roster;
  }

  function makeTonePair(index, frequency) {
    return new Promise((resolve) => {
      const Ctor = window.AudioContext || window.webkitAudioContext;
      let gen = null;
      let sender = null;
      let receiver = null;
      try {
        gen = new Ctor();
        sender = new window.RTCPeerConnection();
        receiver = new window.RTCPeerConnection();
      } catch {
        resolve();
        return;
      }
      const osc = gen.createOscillator();
      osc.frequency.value = frequency;
      const gain = gen.createGain();
      gain.gain.value = 1;
      const dest = gen.createMediaStreamDestination();
      osc.connect(gain);
      gain.connect(dest);
      osc.start();
      testGains[index] = gain;
      const sendTrack = dest.stream.getAudioTracks()[0];
      testPairs.push({ gen, osc, gain, dest, sender, receiver });
      keeps.push({ gen, osc, gain, dest });
      testReceivers.add(receiver);
      let done = false;
      let candidates = 0;
      const finish = () => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        clearInterval(watch);
        resolve();
      };
      const inject = () => {
        attachReceiver({
          track: sendTrack,
          getContributingSources: () => [],
          getSynchronizationSources: () => [],
        });
      };
      const attachReal = () => {
        try {
          const found = receiver.getReceivers ? receiver.getReceivers() : [];
          for (const item of found) {
            if (item && item.track && item.track.kind === "audio") {
              attachReceiver(item);
              return;
            }
          }
        } catch {
          /* fall through to injection */
        }
        inject();
      };
      const timer = setTimeout(() => {
        inject();
        finish();
      }, TEST_PAIR_TIMEOUT_MS);
      const watch = setInterval(() => {
        try {
          const state = receiver.iceConnectionState;
          if (state === "connected" || state === "completed") {
            attachReal();
            finish();
            return;
          }
          if (
            candidates === 0 &&
            sender.iceGatheringState === "complete" &&
            receiver.iceGatheringState === "complete"
          ) {
            inject();
            finish();
          }
        } catch {
          /* keep watching */
        }
      }, 100);
      sender.addEventListener("icecandidate", (event) => {
        if (event.candidate) {
          candidates += 1;
          receiver.addIceCandidate(event.candidate).catch(() => {});
        }
      });
      receiver.addEventListener("icecandidate", (event) => {
        if (event.candidate) {
          candidates += 1;
          sender.addIceCandidate(event.candidate).catch(() => {});
        }
      });
      try {
        sender.addTrack(sendTrack, dest.stream);
      } catch {
        inject();
        finish();
        return;
      }
      sender
        .createOffer()
        .then((offer) => sender.setLocalDescription(offer))
        .then(() => receiver.setRemoteDescription(sender.localDescription))
        .then(() => receiver.createAnswer())
        .then((answer) => receiver.setLocalDescription(answer))
        .then(() => sender.setRemoteDescription(receiver.localDescription))
        .catch(() => {
          inject();
          finish();
        });
      try {
        const resumed = gen.resume();
        if (resumed && typeof resumed.catch === "function") resumed.catch(() => {});
      } catch {
        /* generator locked by the page */
      }
    });
  }

  async function attachTestTones() {
    for (let index = 0; index < TEST_FREQUENCIES.length; index += 1) {
      await makeTonePair(index, TEST_FREQUENCIES[index]);
    }
  }

  function setTestTone(index, value) {
    try {
      const gain = testGains[index];
      if (gain) gain.gain.value = Number(value);
    } catch {
      /* test-only helper */
    }
  }

  function drain() {
    const batch = windows.splice(0, windows.length);
    const bySlot = new Map();
    for (const entry of batch) {
      if (!bySlot.has(entry.slot)) bySlot.set(entry.slot, []);
      bySlot.get(entry.slot).push(entry);
    }
    const streams = [...bySlot.entries()]
      .sort((left, right) => left[0] - right[0])
      .map(([slot, list]) => {
        let total = 0;
        for (const entry of list) total += entry.pcm.length;
        const pcm = new Int16Array(total);
        let offset = 0;
        let level = 0;
        let page_ms = 0;
        const csrcs = [];
        for (const entry of list) {
          pcm.set(entry.pcm, offset);
          offset += entry.pcm.length;
          if (entry.level > level) level = entry.level;
          page_ms = entry.page_ms;
          for (const csrc of entry.csrcs) {
            if (!csrcs.includes(csrc)) csrcs.push(csrc);
          }
        }
        return { slot, csrcs, level, page_ms, pcm_b64: encodeBase64(new Uint8Array(pcm.buffer)) };
      });
    return { streams, roster: participants(), dropped };
  }

  function pending() {
    return windows.length;
  }

  wrapPeerConnection();

  window.__fakeyouStreamCapture = { drain, pending, attachTestTones, setTestTone };
})();
