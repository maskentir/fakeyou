(() => {
  if (window.__fakeyouVoiceActivity) return;
  const SAMPLE_MS = 50;
  const FRAME_WINDOW_MS = 50;
  const FFT_SIZE = 2048;
  const SILENCE_FLOOR = 0.001;
  const MAX_BUFFERED = 6000;
  const MAX_TAPS = 16;
  const instance = Math.random().toString(36).slice(2, 12);
  const taps = [];
  const slots = new Map();
  const localTracks = new Set();
  const levels = [];
  const buffer = new Float32Array(FFT_SIZE);
  const frames = [];
  const frameSlots = new Map();
  const openFrames = new Map();

  function pageNow() {
    try {
      return window.performance.timeOrigin + window.performance.now();
    } catch {
      return Date.now();
    }
  }

  function slotFor(key) {
    if (!slots.has(key)) slots.set(key, slots.size);
    return slots.get(key);
  }

  function streamKey(stream) {
    try {
      const tracks = stream ? stream.getAudioTracks() : [];
      if (tracks.length > 0 && tracks[0].id) return String(tracks[0].id);
      if (stream && stream.id) return String(stream.id);
    } catch {
      /* unreadable stream */
    }
    return "";
  }

  function rememberLocal(stream) {
    try {
      for (const track of stream.getAudioTracks()) localTracks.add(String(track.id));
    } catch {
      /* not a media stream */
    }
    return stream;
  }

  function wrapGetUserMedia() {
    try {
      const devices = navigator.mediaDevices;
      if (!devices || typeof devices.getUserMedia !== "function") return;
      const original = devices.getUserMedia;
      devices.getUserMedia = function (...args) {
        return original.apply(this, args).then(rememberLocal);
      };
    } catch {
      /* page locked media devices */
    }
  }

  function wrapTrackClone() {
    try {
      const proto = window.MediaStreamTrack && window.MediaStreamTrack.prototype;
      if (!proto || typeof proto.clone !== "function") return;
      const original = proto.clone;
      proto.clone = function (...args) {
        const copy = original.apply(this, args);
        try {
          if (localTracks.has(String(this.id))) localTracks.add(String(copy.id));
        } catch {
          /* keep the clone */
        }
        return copy;
      };
    } catch {
      /* page locked tracks */
    }
  }

  function release(tap) {
    try {
      tap.node.disconnect(tap.analyser);
    } catch {
      /* the page already tore the source down */
    }
  }

  function addTap(node, analyser, key) {
    for (let index = taps.length - 1; index >= 0; index -= 1) {
      if (taps[index].key === key) release(taps.splice(index, 1)[0]);
    }
    taps.push({ node, analyser, key, slot: slotFor(key) });
    while (taps.length > MAX_TAPS) release(taps.shift());
  }

  function wrapAudioContext(name) {
    const Original = window[name];
    if (typeof Original !== "function" || Original.__fakeyouVoiceWrapped) return;
    function Wrapped(...args) {
      const ctx = new Original(...args);
      try {
        const create = ctx.createMediaStreamSource;
        if (typeof create === "function") {
          ctx.createMediaStreamSource = function (stream) {
            const node = create.call(this, stream);
            try {
              const key = streamKey(stream);
              if (key) {
                const analyser = ctx.createAnalyser();
                analyser.fftSize = FFT_SIZE;
                node.connect(analyser);
                addTap(node, analyser, key);
              }
            } catch {
              /* tapping must never break audio */
            }
            return node;
          };
        }
      } catch {
        /* context without media sources */
      }
      return ctx;
    }
    Wrapped.prototype = Original.prototype;
    Object.setPrototypeOf(Wrapped, Original);
    Wrapped.__fakeyouVoiceWrapped = true;
    try {
      window[name] = Wrapped;
    } catch {
      /* page locked the constructor */
    }
  }

  function frameSlotFor(key) {
    if (!frameSlots.has(key)) frameSlots.set(key, frameSlots.size);
    return frameSlots.get(key);
  }

  function noteFrame(slot, frame) {
    const meta = frame.getMetadata();
    const bucket = Math.floor(pageNow() / FRAME_WINDOW_MS) * FRAME_WINDOW_MS;
    let entry = openFrames.get(slot);
    if (!entry || entry[0] !== bucket) {
      entry = [bucket, slot, 0, []];
      openFrames.set(slot, entry);
      frames.push(entry);
      if (frames.length > MAX_BUFFERED) frames.splice(0, frames.length - MAX_BUFFERED);
    }
    const level = Number(meta.audioLevel);
    if (Number.isFinite(level) && level > entry[2]) entry[2] = level;
    for (const source of meta.contributingSources || []) {
      const csrc = Number(source);
      if (Number.isFinite(csrc) && !entry[3].includes(csrc)) entry[3].push(csrc);
    }
  }

  function wrapEncodedStreams() {
    try {
      const proto = window.RTCRtpReceiver && window.RTCRtpReceiver.prototype;
      if (!proto || typeof proto.createEncodedStreams !== "function") return;
      const original = proto.createEncodedStreams;
      proto.createEncodedStreams = function (...args) {
        const streams = original.apply(this, args);
        try {
          if (!this.track || this.track.kind !== "audio") return streams;
          const slot = frameSlotFor(String(this.track.id));
          const tap = new TransformStream({
            transform(frame, controller) {
              try {
                noteFrame(slot, frame);
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
      /* page locked receivers */
    }
  }

  function drainFrames() {
    openFrames.clear();
    return frames.splice(0, frames.length);
  }

  function sample() {
    const stamp = pageNow();
    for (const tap of taps) {
      if (localTracks.has(tap.key)) continue;
      try {
        tap.analyser.getFloatTimeDomainData(buffer);
        let sum = 0;
        for (let index = 0; index < buffer.length; index += 1) {
          sum += buffer[index] * buffer[index];
        }
        const level = Math.sqrt(sum / buffer.length);
        if (level >= SILENCE_FLOOR) levels.push([stamp, tap.slot, level]);
      } catch {
        continue;
      }
    }
    if (levels.length > MAX_BUFFERED) levels.splice(0, levels.length - MAX_BUFFERED);
  }

  function localSlots() {
    return taps.filter((tap) => localTracks.has(tap.key)).map((tap) => tap.slot);
  }

  wrapGetUserMedia();
  wrapEncodedStreams();
  wrapTrackClone();
  wrapAudioContext("AudioContext");
  wrapAudioContext("webkitAudioContext");
  setInterval(() => {
    try {
      sample();
    } catch {
      /* sampler must never break the page */
    }
  }, SAMPLE_MS);

  window.__fakeyouVoiceActivity = {
    drain() {
      const batch = {
        instance,
        page_ms: pageNow(),
        levels: levels.splice(0, levels.length),
        frames: drainFrames(),
        local: [],
      };
      try {
        batch.local = localSlots();
      } catch {
        /* local slots unavailable this round */
      }
      return batch;
    },
  };
})();
