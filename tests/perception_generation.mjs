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
