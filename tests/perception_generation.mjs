import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

let onTrack;
const readers = [];
const bridge = {
  peers: [],
  pageNow: () => 1000,
  onDataChannel: () => {},
  onTrack: (callback) => { onTrack = callback; },
};
class Processor {
  readable = {
    getReader: () => ({
      read: () => new Promise((resolve) => readers.push(resolve)),
      cancel: () => Promise.resolve(),
    }),
  };
}
const window = { __fakeyouRtc: bridge };
vm.runInNewContext(readFileSync(process.argv[2], "utf8"), {
  window,
  crypto: { randomUUID: () => "page-session-a" },
  MediaStreamTrackProcessor: Processor,
});
const api = window.__fakeyouPerception;
function track() {
  const listeners = new Map();
  return {
    kind: "video",
    readyState: "live",
    clone: () => ({ stop: () => {} }),
    addEventListener: (name, callback) => listeners.set(name, callback),
    end: () => listeners.get("ended")(),
  };
}
const first = track();
api.assign([{ stream_id: "share", role: "camera", device_id: "person-a" }]);
onTrack({ track: first, streams: [{ id: "share" }] });
const initial = api.drain().active[0].generation;
api.assign([{ stream_id: "share", role: "camera", device_id: "person-a" }]);
assert.equal(api.drain().active[0].generation, initial);
api.assign([{ stream_id: "share", role: "camera", device_id: "person-b" }]);
const reassigned = api.drain().active[0].generation;
assert.ok(reassigned > initial);
readers[0]({ done: true });
await Promise.resolve();
await Promise.resolve();
assert.equal(api.drain().active[0].generation, reassigned);
const second = track();
onTrack({ track: second, streams: [{ id: "share" }] });
const replaced = api.drain().active[0].generation;
assert.ok(replaced > reassigned);
first.end();
assert.equal(api.drain().active[0].generation, replaced);
api.assign([]);
assert.equal(api.drain().active.length, 0);

const windowB = { __fakeyouRtc: bridge };
vm.runInNewContext(readFileSync(process.argv[2], "utf8"), {
  window: windowB,
  crypto: { randomUUID: () => "page-session-b" },
  MediaStreamTrackProcessor: Processor,
});
const apiB = windowB.__fakeyouPerception;
apiB.assign([{ stream_id: "share", role: "camera", device_id: "person-a" }]);
onTrack({ track: track(), streams: [{ id: "share" }] });
const reloaded = apiB.drain();
assert.equal(reloaded.active[0].generation, 1);
assert.ok(replaced > reloaded.active[0].generation);
assert.equal(reloaded.page_session, "page-session-b");
assert.equal(reloaded.active[0].page_session, reloaded.page_session);
assert.notEqual(api.drain().page_session, reloaded.page_session);
apiB.assign([]);

const windowC = { __fakeyouRtc: bridge };
vm.runInNewContext(readFileSync(process.argv[2], "utf8"), {
  window: windowC,
  crypto: { getRandomValues: (bytes) => bytes.fill(171) },
  Uint8Array,
  MediaStreamTrackProcessor: Processor,
});
const insecure = windowC.__fakeyouPerception.drain();
assert.equal(insecure.page_session, "ab".repeat(16));
assert.notEqual(insecure.page_session, reloaded.page_session);
