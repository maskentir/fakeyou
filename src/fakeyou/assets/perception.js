(() => {
  if (window.__fakeyouPerception) return;
  const bridge = window.__fakeyouRtc;
  if (!bridge) return;
  const PROTOCOL_CHANNELS = ["collections"];
  const MAX_MESSAGES = 2000;
  const MAX_FRAMES = 8;
  const MAX_DRAIN_BYTES = 6000000;
  const SAMPLE_MS = { screen: 500, camera: 4000 };
  const MAX_EDGE = { screen: 1920, camera: 480 };
  const JPEG_QUALITY = 0.85;
  const THUMB_WIDTH = 128;
  const THUMB_HEIGHT = 72;
  const BLOCK = 8;
  const BLOCK_CHANGE = 4;
  const SETTLE_SAMPLES = 2;
  const EMIT_CHANGED_BLOCKS = { screen: 2, camera: 12 };
  const MOVING_KEYFRAME_MS = 15000;
  const CAMERA_REFRESH_MS = 120000;
  const messages = [];
  const frames = [];
  const videoTracks = new Map();
  const roles = new Map();
  const samplers = new Map();
  const channels = [];
  let droppedMessages = 0;
  let droppedFrames = 0;
  let thumbCanvas = null;

  function encodeBase64(bytes) {
    let text = "";
    for (let index = 0; index < bytes.length; index += 0x8000) {
      text += String.fromCharCode.apply(null, bytes.subarray(index, index + 0x8000));
    }
    return btoa(text);
  }

  function pushMessage(label, bytes) {
    messages.push({ channel: label, page_ms: bridge.pageNow(), b64: encodeBase64(bytes) });
    if (messages.length > MAX_MESSAGES) {
      messages.splice(0, messages.length - MAX_MESSAGES);
      droppedMessages += 1;
    }
  }

  function onChannelMessage(label, event) {
    const data = event ? event.data : null;
    if (data instanceof ArrayBuffer) {
      pushMessage(label, new Uint8Array(data));
    } else if (ArrayBuffer.isView(data)) {
      pushMessage(label, new Uint8Array(data.buffer, data.byteOffset, data.byteLength));
    } else if (data instanceof Blob) {
      data
        .arrayBuffer()
        .then((buffer) => pushMessage(label, new Uint8Array(buffer)))
        .catch(() => {});
    }
  }

  bridge.onDataChannel((channel) => {
    const label = String(channel.label || "");
    if (!PROTOCOL_CHANNELS.includes(label)) return;
    channels.push(label);
    channel.addEventListener("message", (event) => {
      try {
        onChannelMessage(label, event);
      } catch {
        /* reading must never break the page */
      }
    });
  });

  function thumbnail(frame) {
    if (!thumbCanvas) thumbCanvas = new OffscreenCanvas(THUMB_WIDTH, THUMB_HEIGHT);
    const context = thumbCanvas.getContext("2d", { willReadFrequently: true });
    context.drawImage(frame, 0, 0, THUMB_WIDTH, THUMB_HEIGHT);
    const rgba = context.getImageData(0, 0, THUMB_WIDTH, THUMB_HEIGHT).data;
    const gray = new Uint8Array(THUMB_WIDTH * THUMB_HEIGHT);
    for (let pixel = 0; pixel < gray.length; pixel += 1) {
      const offset = pixel * 4;
      gray[pixel] = (rgba[offset] * 299 + rgba[offset + 1] * 587 + rgba[offset + 2] * 114) / 1000;
    }
    return gray;
  }

  function changedBlocks(left, right) {
    if (!left || !right) return Infinity;
    let changed = 0;
    for (let top = 0; top < THUMB_HEIGHT; top += BLOCK) {
      for (let column = 0; column < THUMB_WIDTH; column += BLOCK) {
        let total = 0;
        for (let row = top; row < top + BLOCK; row += 1) {
          for (let x = column; x < column + BLOCK; x += 1) {
            const index = row * THUMB_WIDTH + x;
            total += Math.abs(left[index] - right[index]);
          }
        }
        if (total / (BLOCK * BLOCK) > BLOCK_CHANGE) changed += 1;
      }
    }
    return changed;
  }

  function pushFrame(entry) {
    frames.push(entry);
    if (frames.length > MAX_FRAMES) {
      const index = frames.findIndex((item) => item.role === "camera");
      frames.splice(index >= 0 ? index : 0, 1);
      droppedFrames += 1;
    }
  }

  function emit(state, frame, reason) {
    const sourceWidth = frame.displayWidth || frame.codedWidth;
    const sourceHeight = frame.displayHeight || frame.codedHeight;
    if (!sourceWidth || !sourceHeight) return;
    const scale = Math.min(1, MAX_EDGE[state.role] / Math.max(sourceWidth, sourceHeight));
    const width = Math.max(1, Math.round(sourceWidth * scale));
    const height = Math.max(1, Math.round(sourceHeight * scale));
    const canvas = new OffscreenCanvas(width, height);
    canvas.getContext("2d").drawImage(frame, 0, 0, width, height);
    const page_ms = bridge.pageNow();
    canvas
      .convertToBlob({ type: "image/jpeg", quality: JPEG_QUALITY })
      .then((blob) => blob.arrayBuffer())
      .then((buffer) =>
        pushFrame({
          stream_id: state.id,
          role: state.role,
          page_ms,
          width,
          height,
          reason,
          jpeg_b64: encodeBase64(new Uint8Array(buffer)),
        })
      )
      .catch(() => {});
  }

  function sampleScreen(state, frame, thumb, now) {
    const moved = changedBlocks(thumb, state.lastThumb) > 0;
    state.lastThumb = thumb;
    if (moved) {
      state.stableSamples = 0;
      if (!state.changingSince) state.changingSince = now;
      if (now - state.changingSince >= MOVING_KEYFRAME_MS) {
        state.changingSince = now;
        state.emittedThumb = thumb;
        emit(state, frame, "moving");
      }
      return;
    }
    state.stableSamples += 1;
    if (state.stableSamples !== SETTLE_SAMPLES) return;
    state.changingSince = 0;
    if (changedBlocks(thumb, state.emittedThumb) >= EMIT_CHANGED_BLOCKS.screen) {
      state.emittedThumb = thumb;
      emit(state, frame, "settled");
    }
  }

  function sampleCamera(state, frame, thumb, now) {
    const changed = changedBlocks(thumb, state.emittedThumb) >= EMIT_CHANGED_BLOCKS.camera;
    if (changed || now - state.emittedAt >= CAMERA_REFRESH_MS) {
      state.emittedThumb = thumb;
      state.emittedAt = now;
      emit(state, frame, changed ? "changed" : "refresh");
    }
  }

  function sample(state, frame, now) {
    const thumb = thumbnail(frame);
    if (state.role === "screen") sampleScreen(state, frame, thumb, now);
    else sampleCamera(state, frame, thumb, now);
  }

  async function pump(state) {
    const interval = SAMPLE_MS[state.role];
    while (!state.stopped) {
      let result = null;
      try {
        result = await state.reader.read();
      } catch {
        break;
      }
      if (!result || result.done) break;
      const frame = result.value;
      try {
        const now = performance.now();
        if (now - state.sampledAt >= interval) {
          state.sampledAt = now;
          sample(state, frame, now);
        }
      } catch {
        /* sampling must never break the page */
      } finally {
        try {
          frame.close();
        } catch {
          /* frame already closed */
        }
      }
    }
    stop(state.id);
  }

  function stop(id) {
    const state = samplers.get(id);
    if (!state) return;
    samplers.delete(id);
    state.stopped = true;
    try {
      state.reader.cancel();
    } catch {
      /* reader already gone */
    }
    try {
      state.clone.stop();
    } catch {
      /* clone already stopped */
    }
  }

  function start(id) {
    const role = roles.get(id);
    const entry = videoTracks.get(id);
    if (!role || !entry || samplers.has(id) || entry.track.readyState === "ended") return;
    if (typeof MediaStreamTrackProcessor !== "function") return;
    const clone = entry.track.clone();
    const reader = new MediaStreamTrackProcessor({ track: clone }).readable.getReader();
    const state = {
      id,
      role,
      clone,
      reader,
      stopped: false,
      sampledAt: 0,
      lastThumb: null,
      emittedThumb: null,
      emittedAt: 0,
      stableSamples: 0,
      changingSince: 0,
    };
    samplers.set(id, state);
    void pump(state);
  }

  bridge.onTrack((entry) => {
    const track = entry.track;
    if (!track || track.kind !== "video") return;
    for (const stream of entry.streams) {
      const id = String((stream && stream.id) || "");
      if (!id) continue;
      videoTracks.set(id, { track });
      track.addEventListener(
        "ended",
        () => {
          videoTracks.delete(id);
          stop(id);
        },
        { once: true }
      );
      start(id);
    }
  });

  function assign(assignments) {
    const wanted = new Map();
    for (const item of assignments || []) {
      if (item && (item.role === "screen" || item.role === "camera")) {
        wanted.set(String(item.stream_id), item.role);
      }
    }
    for (const id of [...samplers.keys()]) {
      const state = samplers.get(id);
      if (!wanted.has(id) || wanted.get(id) !== state.role) stop(id);
    }
    roles.clear();
    for (const [id, role] of wanted) {
      roles.set(id, role);
      start(id);
    }
    return { sampling: [...samplers.keys()], known: [...videoTracks.keys()] };
  }

  function takeWithin(queue, budget, size) {
    const taken = [];
    let used = 0;
    while (queue.length) {
      const next = size(queue[0]);
      if (taken.length && used + next > budget) break;
      taken.push(queue.shift());
      used += next;
    }
    return { taken, used };
  }

  function peerStates() {
    return bridge.peers.map((peer) => {
      try {
        return String(peer.connectionState || "new");
      } catch {
        return "unknown";
      }
    });
  }

  function drain() {
    const sizedMessages = takeWithin(messages, MAX_DRAIN_BYTES, (item) => item.b64.length);
    const sizedFrames = takeWithin(
      frames,
      Math.max(0, MAX_DRAIN_BYTES - sizedMessages.used),
      (item) => item.jpeg_b64.length
    );
    return {
      messages: sizedMessages.taken,
      frames: sizedFrames.taken,
      tracks: [...videoTracks.keys()],
      sampling: [...samplers.keys()],
      channels: channels.slice(),
      peers: peerStates(),
      dropped_messages: droppedMessages,
      dropped_frames: droppedFrames,
    };
  }

  window.__fakeyouPerception = { drain, assign };
})();
