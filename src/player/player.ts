// Picture-in-picture player: runs inside a Steam browser view (not the Decky context), loaded from the
// plugin's local HTTP server. It never talks to Home Assistant directly and holds no credential: WebRTC
// and HLS signalling goes through the plugin backend (loopback HTTP long-poll), which relays it to the
// Home Assistant integration, which talks to the camera.
import Hls from "hls.js/light";

const params = new URLSearchParams(location.search);
const entityId = params.get("entity") ?? "";
const nonce = params.get("nonce") ?? "";
const session = params.get("session") ?? "";
const muted = params.get("mute") !== "0";
// Per-show token echoed in the title so the host can ignore a previous page's state
const showToken = params.get("t") ?? "";

const video = document.getElementById("video") as HTMLVideoElement;
const statusEl = document.getElementById("status") as HTMLElement;

// The page title is readable from the Steam browser view, so it doubles as state:
// "<state>[: <message>] [@<aspect ratio>] #<show token>"
let lastState = "connecting";
let lastMessage = "";
const publishState = () => {
  const aspect = video.videoWidth && video.videoHeight ? ` @${(video.videoWidth / video.videoHeight).toFixed(4)}` : "";
  const text = lastMessage ? `${lastState}: ${lastMessage}` : lastState;
  document.title = `${text}${aspect} #${showToken}`;
};
const setState = (state: string, message = "") => {
  lastState = state;
  lastMessage = message;
  statusEl.textContent = state === "playing" ? "" : message || state;
  publishState();
};
// The stream's resolution is only known once video data arrives, and can change
video.addEventListener("loadedmetadata", publishState);
video.addEventListener("resize", publishState);

// --- signalling through the plugin backend -----------------------------------

type Message = { kind: string; [key: string]: any };

const query = `nonce=${encodeURIComponent(nonce)}&session=${encodeURIComponent(session)}`;
let sendChain: Promise<unknown> = Promise.resolve();

/** Messages to the integration are sent one after another so candidates never overtake the offer. */
const send = (message: Message): Promise<unknown> => {
  sendChain = sendChain
    .catch(() => {})
    .then(() => fetch(`/pip/send?${query}`, { method: "POST", body: JSON.stringify(message) }))
    .then((res) => {
      if (!res.ok) throw new Error(`signalling failed (${res.status})`);
    });
  return sendChain;
};

let stopped = false;
const poll = async (onMessage: (m: Message) => void) => {
  let after = 0;
  while (!stopped) {
    try {
      const res = await fetch(`/pip/events?${query}&after=${after}`, { cache: "no-store" });
      if (!res.ok) throw new Error(`signalling failed (${res.status})`);
      const body: { messages: (Message & { seq: number })[]; next: number; closed: boolean } = await res.json();
      after = body.next;
      body.messages.forEach(onMessage);
      if (body.closed) return;
    } catch (e) {
      if (stopped) return;
      setState("error", e instanceof Error ? e.message : String(e));
      return;
    }
  }
};

// --- WebRTC first, HLS as the fallback -----------------------------------------

// WebRTC is tried first (low latency). Streams it cannot carry, such as H.265 cameras, or any failure fall
// back to Home Assistant's HLS stream, which this browser engine plays through MSE.
const WEBRTC_TIMEOUT_MS = 10000;

let transport: "webrtc" | "hls" = "webrtc";
let pc: RTCPeerConnection | undefined;
let hls: Hls | undefined;
let webrtcTimer: ReturnType<typeof setTimeout> | undefined;
let failedOver = false;

// Chromium refuses unmuted autoplay without user activation. The plugin grants activation to this view; if that
// is not available (older Steam builds) fall back to muted playback instead of showing nothing.
let soundBlocked = false;
const ACTIVATION_RETRIES = 10;
const ACTIVATION_RETRY_MS = 300;
const playVideo = (attempt = 0) => {
  video.play().catch((e) => {
    if (e?.name !== "NotAllowedError" || video.muted) return;
    // The plugin grants activation while the page is not playing yet; give it a moment before giving up on sound
    if (attempt < ACTIVATION_RETRIES) return void setTimeout(() => playVideo(attempt + 1), ACTIVATION_RETRY_MS);
    soundBlocked = true;
    video.muted = true;
    video.play().catch(() => {});
  });
};

const stopWebRtc = () => {
  clearTimeout(webrtcTimer);
  pc?.close();
  pc = undefined;
  video.srcObject = null;
};

const fail = (reason: string) => {
  if (failedOver || transport === "hls") return;
  failedOver = true;
  console.warn("Decky Home Assistant PiP: WebRTC failed:", reason);
  stopWebRtc();
  transport = "hls";
  setState("negotiating", `Falling back to HLS (${reason})`);
  send({ kind: "hls" }).catch((e) => setState("error", e instanceof Error ? e.message : String(e)));
};

const startWebRtc = async (config: RTCConfiguration, dataChannel?: string) => {
  const peer = new RTCPeerConnection(config);
  pc = peer;
  peer.addTransceiver("video", { direction: "recvonly" });
  peer.addTransceiver("audio", { direction: "recvonly" });
  if (dataChannel) peer.createDataChannel(dataChannel);
  peer.ontrack = (e) => {
    if (video.srcObject !== e.streams[0]) video.srcObject = e.streams[0];
    playVideo();
  };
  peer.onconnectionstatechange = () => {
    if (peer.connectionState === "failed") fail("WebRTC connection failed");
  };
  peer.onicecandidate = (e) => {
    if (e.candidate) send({ kind: "candidate", candidate: e.candidate.toJSON() }).catch(() => {});
  };
  const offer = await peer.createOffer();
  await peer.setLocalDescription(offer);
  await send({ kind: "offer", sdp: offer.sdp });
  setState("negotiating", "Waiting for video");
  webrtcTimer = setTimeout(() => fail("WebRTC timed out"), WEBRTC_TIMEOUT_MS);
};

const startHls = (url: string) => {
  if (!Hls.isSupported()) return setState("error", "HLS is not supported in this browser");
  const player = new Hls({ lowLatencyMode: true, backBufferLength: 5 });
  hls = player;
  player.on(Hls.Events.ERROR, (_e, data) => {
    if (data.fatal) setState("error", `HLS ${data.type}: ${data.details}`);
  });
  player.loadSource(url);
  player.attachMedia(video);
  playVideo();
};

const onMessage = (m: Message) => {
  switch (m.kind) {
    case "config":
      if (!m.webrtc) return fail("WebRTC not supported by this camera");
      startWebRtc(m.config?.configuration ?? {}, m.config?.dataChannel).catch((e) =>
        fail(e instanceof Error ? e.message : String(e)),
      );
      break;
    case "answer":
      pc?.setRemoteDescription({ type: "answer", sdp: m.answer }).catch((e) => fail(String(e)));
      break;
    case "candidate":
      pc?.addIceCandidate(m.candidate).catch(() => {});
      break;
    case "hls_url":
      startHls(m.url);
      break;
    case "error":
      // A WebRTC failure falls back; anything after that is final
      if (transport === "webrtc") fail(String(m.message ?? m.code ?? "error").split("\n")[0]);
      else setState("error", String(m.message ?? m.code ?? "error"));
      break;
  }
};

video.addEventListener("playing", () => {
  clearTimeout(webrtcTimer);
  setState("playing", [transport === "hls" ? "HLS" : "", soundBlocked ? "muted (sound blocked)" : ""].filter(Boolean).join(", "));
});

window.addEventListener("unload", () => {
  stopped = true;
  stopWebRtc();
  hls?.destroy();
  navigator.sendBeacon?.(`/pip/send?${query}`, JSON.stringify({ kind: "close" }));
});

if (!entityId.startsWith("camera.") || !session) {
  setState("error", "missing or invalid camera or session");
} else {
  video.muted = muted;
  setState("connecting", "Connecting");
  poll(onMessage);
  send({ kind: "start", camera: entityId }).catch((e) => setState("error", e instanceof Error ? e.message : String(e)));
}
