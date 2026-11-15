import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { iceGathered, newPeer, pause, rtcSupported } from "../api/rtc";

/*
 * The dispatcher calls her: microphone → offer → the phone answers by itself → both talk live.
 * The server stops the assistant when the offer arrives. Push to talk stays as the fallback.
 */

export type DispatcherCallState = "idle" | "calling" | "ringing" | "live" | "ended" | "failed";

const ANSWER_WAIT_MS = 20_000;

export function useDispatcherCall(sessionId: string, dashboardKey: string | null, active: boolean) {
  const [state, setState] = useState<DispatcherCallState>("idle");
  const [error, setError] = useState<string | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const peer = useRef<RTCPeerConnection | null>(null);
  const mic = useRef<MediaStream | null>(null);
  const run = useRef(0);
  const key = dashboardKey ?? undefined;

  const teardown = useCallback(() => {
    run.current += 1;
    peer.current?.close();
    peer.current = null;
    mic.current?.getTracks().forEach((track) => track.stop());
    mic.current = null;
    if (audioRef.current) audioRef.current.srcObject = null;
  }, []);

  const fail = useCallback(
    (message: string) => {
      teardown();
      setState("failed");
      setError(message);
    },
    [teardown],
  );

  const start = useCallback(async (): Promise<boolean> => {
    if (!rtcSupported()) {
      fail("Browserul nu poate face apeluri. Folosește mesajul vocal.");
      return false;
    }
    teardown();
    const mine = run.current;
    setState("calling");
    setError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      if (mine !== run.current) {
        stream.getTracks().forEach((track) => track.stop());
        return false;
      }
      mic.current = stream;
      const pc = newPeer();
      peer.current = pc;
      stream.getAudioTracks().forEach((track) => pc.addTrack(track, stream));
      pc.ontrack = (event) => {
        const audio = audioRef.current;
        if (!audio) return;
        audio.srcObject = event.streams[0] ?? new MediaStream([event.track]);
        void audio.play().catch(() => undefined);
      };
      pc.onconnectionstatechange = () => {
        if (mine !== run.current) return;
        if (pc.connectionState === "connected") setState("live");
        else if (pc.connectionState === "failed") fail("Legătura cu telefonul s-a întrerupt. Sună din nou sau folosește mesajul vocal.");
      };
      await pc.setLocalDescription(await pc.createOffer());
      await iceGathered(pc);
      if (mine !== run.current) return false;
      await api.callOffer(sessionId, pc.localDescription?.sdp ?? "", key);
      setState((current) => (current === "calling" ? "ringing" : current));
      const deadline = Date.now() + ANSWER_WAIT_MS;
      while (mine === run.current && Date.now() < deadline) {
        const view = await api.callAnswerPoll(sessionId, key).catch(() => null);
        if (mine !== run.current) return false;
        if (view?.answer) {
          await pc.setRemoteDescription({ type: "answer", sdp: view.answer });
          return true;
        }
        if (view && view.call_id === null) break;
        await pause(700);
      }
      if (mine === run.current) fail("Telefonul nu a răspuns la apel. Sună din nou sau folosește mesajul vocal.");
      return false;
    } catch (failure) {
      if (mine !== run.current) return false;
      const denied = failure instanceof DOMException && failure.name === "NotAllowedError";
      fail(denied ? "Microfonul nu este permis în acest browser." : "Apelul nu a pornit. Sună din nou sau folosește mesajul vocal.");
      return false;
    }
  }, [sessionId, key, fail, teardown]);

  const hangup = useCallback(async () => {
    teardown();
    setState("ended");
    setError(null);
    await api.callHangup(sessionId, key).catch(() => undefined);
  }, [sessionId, key, teardown]);

  // The session ended (she is safe, false alarm…): the call ends with it.
  useEffect(() => {
    if (active) return;
    teardown();
    setState((current) => (current === "idle" ? current : "ended"));
  }, [active, teardown]);
  useEffect(() => teardown, [teardown]);

  return { state, error, start, hangup, audioRef } as const;
}
