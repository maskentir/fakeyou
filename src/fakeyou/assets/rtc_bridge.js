(() => {
  if (window.__fakeyouRtc) return;
  const MAX_REMEMBERED_TRACKS = 500;
  const peers = [];
  const tracks = [];
  const channels = [];
  const trackSubscribers = [];
  const channelSubscribers = [];

  function pageNow() {
    try {
      return window.performance.timeOrigin + window.performance.now();
    } catch {
      return Date.now();
    }
  }

  function notify(subscribers, value) {
    for (const subscriber of subscribers) {
      try {
        subscriber(value);
      } catch {
        /* a subscriber must never break the page */
      }
    }
  }

  function noteTrack(event) {
    try {
      const receiver = event ? event.receiver || null : null;
      const track = event ? event.track || (receiver ? receiver.track : null) : null;
      if (!track) return;
      const entry = {
        peer: event.target || event.currentTarget || null,
        receiver,
        track,
        streams: Array.from(event.streams || []),
      };
      tracks.push(entry);
      if (tracks.length > MAX_REMEMBERED_TRACKS) tracks.splice(0, tracks.length - MAX_REMEMBERED_TRACKS);
      notify(trackSubscribers, entry);
    } catch {
      /* tapping must never break the page */
    }
  }

  function noteChannel(channel) {
    if (!channel || channels.includes(channel)) return;
    channels.push(channel);
    notify(channelSubscribers, channel);
  }

  function watch(peer) {
    peers.push(peer);
    try {
      peer.addEventListener("track", noteTrack);
    } catch {
      /* page locked the peer */
    }
    try {
      peer.addEventListener("datachannel", (event) => noteChannel(event ? event.channel : null));
    } catch {
      /* page locked the peer */
    }
  }

  function wrapCreateDataChannel(prototype) {
    try {
      const original = prototype && prototype.createDataChannel;
      if (typeof original !== "function" || original.__fakeyouBridgeWrapped) return;
      const wrapped = function (...args) {
        const channel = original.apply(this, args);
        noteChannel(channel);
        return channel;
      };
      wrapped.__fakeyouBridgeWrapped = true;
      prototype.createDataChannel = wrapped;
    } catch {
      /* page locked the prototype */
    }
  }

  function wrapPeerConnection() {
    const Original = window.RTCPeerConnection;
    if (typeof Original !== "function" || Original.__fakeyouBridgeWrapped) return;
    function Wrapped(...args) {
      const peer = new Original(...args);
      watch(peer);
      return peer;
    }
    Wrapped.prototype = Original.prototype;
    Object.setPrototypeOf(Wrapped, Original);
    Wrapped.__fakeyouBridgeWrapped = true;
    wrapCreateDataChannel(Original.prototype);
    try {
      window.RTCPeerConnection = Wrapped;
    } catch {
      /* page locked the constructor */
    }
  }

  function replay(subscriber, remembered) {
    for (const value of remembered.slice()) {
      try {
        subscriber(value);
      } catch {
        /* a subscriber must never break the page */
      }
    }
  }

  wrapPeerConnection();

  window.__fakeyouRtc = {
    peers,
    pageNow,
    onTrack(subscriber) {
      trackSubscribers.push(subscriber);
      replay(subscriber, tracks);
    },
    onDataChannel(subscriber) {
      channelSubscribers.push(subscriber);
      replay(subscriber, channels);
    },
  };
  if (window.__fakeyouTestHooks === true) {
    window.__fakeyouRtc.emitTestTrack = (track, streams) =>
      noteTrack({ track, streams, receiver: null, target: null });
    window.__fakeyouRtc.emitTestChannel = (channel) => noteChannel(channel);
  }
})();
