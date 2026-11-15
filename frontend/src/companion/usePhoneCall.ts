import { useEffect, useState, type MutableRefObject } from "react";
import { api } from "../api/client";
import { iceGathered, newPeer, rtcSupported } from "../api/rtc";
import type { AudioEngine } from "./audioEngine";
import type { Credential } from "./useCompanionSession";

/*
 * The dispatcher calls: her phone answers by itself (she must not touch it). Her microphone goes
 * to the dispatcher; the dispatcher's voice plays in her earbuds and goes into the recording.
 */

export type PhoneCallState = "idle" | "connecting" | "live";

const POLL_MS = 1200;

export function usePhoneCall({
  engine,
  credential,
  active,
  ensureMic,
}: {
  engine: MutableRefObject<AudioEngine>;
  credential: Credential | null;
  active: boolean;
  /** Prepares the microphone when it is not running yet (permission already given). */
  ensureMic: () => Promise<boolean>;
}): PhoneCallState {
  const [state, setState] = useState<PhoneCallState>("idle");
  const sessionId = credential?.sessionId ?? null;
  const token = credential?.token ?? null;

  useEffect(() => {
    if (!sessionId || !token || !active || !rtcSupported()) return;
    let stopped = false;
    let busy = false;
    let call: { id: string; pc: RTCPeerConnection; stopSound: () => void } | null = null;

    const close = () => {
      if (!call) return;
      call.stopSound();
      call.pc.close();
      call = null;
      if (!stopped) setState("idle");
    };

    const answer = async (callId: string, offer: string) => {
      close();
      if (!engine.current.micStream) await ensureMic();
      const mic = engine.current.micStream;
      if (stopped || !mic) return;
      const pc = newPeer();
      const current: { id: string; pc: RTCPeerConnection; stopSound: () => void } = { id: callId, pc, stopSound: () => undefined };
      call = current;
      mic.getAudioTracks().forEach((track) => pc.addTrack(track, mic));
      pc.ontrack = (event) => {
        current.stopSound();
        current.stopSound = engine.current.playRemote(event.streams[0] ?? new MediaStream([event.track]));
      };
      pc.onconnectionstatechange = () => {
        if (call !== current) return;
        if (pc.connectionState === "connected") setState("live");
        else if (pc.connectionState === "failed" || pc.connectionState === "closed") close();
      };
      setState("connecting");
      await pc.setRemoteDescription({ type: "offer", sdp: offer });
      await pc.setLocalDescription(await pc.createAnswer());
      await iceGathered(pc);
      if (stopped || call !== current) return;
      await api.callAnswer(sessionId, token, callId, pc.localDescription?.sdp ?? "");
    };

    const poll = async () => {
      if (busy || stopped) return;
      busy = true;
      try {
        const view = await api.callForPhone(sessionId, token);
        if (stopped) return;
        if (!view.call_id) close();
        else if (view.call_id !== call?.id && view.offer) await answer(view.call_id, view.offer);
      } catch {
        // next tick; a broken answer is closed so the dispatcher can call again
        if (call && call.pc.connectionState !== "connected") close();
      } finally {
        busy = false;
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), POLL_MS);
    return () => {
      stopped = true;
      window.clearInterval(timer);
      close();
      setState("idle");
    };
  }, [sessionId, token, active, engine, ensureMic]);

  return state;
}
