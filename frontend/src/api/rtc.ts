/*
 * Live call between the dispatcher and her phone (WebRTC, audio only). The server passes the
 * offer and the answer; the sound itself goes directly between the two devices on the network.
 * Candidates are gathered before sending (no trickle), so one offer and one answer are enough.
 */

export const rtcSupported = (): boolean => typeof window !== "undefined" && typeof window.RTCPeerConnection === "function";

export function newPeer(): RTCPeerConnection {
  // Same network (hotspot / LAN): host candidates are enough, no outside server is contacted.
  return new RTCPeerConnection({ iceServers: [] });
}

/** Resolves when all candidates are in the local description, or after `ms`. */
export function iceGathered(pc: RTCPeerConnection, ms = 3000): Promise<void> {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      window.clearTimeout(timer);
      pc.removeEventListener("icegatheringstatechange", check);
      resolve();
    };
    const check = () => {
      if (pc.iceGatheringState === "complete") done();
    };
    const timer = window.setTimeout(done, ms);
    pc.addEventListener("icegatheringstatechange", check);
  });
}

export const pause = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));
