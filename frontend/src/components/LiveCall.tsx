import { BellOff, BellRing, Headphones, Mic, PhoneCall, PhoneOff, Radio, RotateCcw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { apiBaseUrl } from "../api/config";
import { isUnfinished, type ConversationResponse, type SessionView } from "../api/contract";
import { formatClock } from "../state/presentation";
import { useDispatcherCall, type DispatcherCallState } from "./useDispatcherCall";

/*
 * The dispatcher's call panel. When a session becomes an alert the dashboard rings; the
 * dispatcher can listen (the recording from its first second, then live) and take the call:
 * the assistant in her earbuds goes quiet and the dispatcher talks with her live (WebRTC).
 * Push to talk stays as the fallback when the live call cannot connect.
 */

const MSE_TYPE = 'audio/webm; codecs="opus"';

let soundContext: AudioContext | null = null;
function audioContext(): AudioContext | null {
  try {
    soundContext ??= new AudioContext();
    return soundContext;
  } catch {
    return null;
  }
}

function useRinger(ringing: boolean) {
  const [enabled, setEnabled] = useState(() => audioContext()?.state === "running");
  useEffect(() => {
    if (!ringing || !enabled) return;
    const ctx = audioContext();
    if (!ctx) return;
    const ring = () => {
      [0, 0.25, 0.5, 0.75].forEach((offset, index) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.frequency.value = index % 2 === 0 ? 880 : 660;
        gain.gain.setValueAtTime(0.0001, ctx.currentTime + offset);
        gain.gain.exponentialRampToValueAtTime(0.25, ctx.currentTime + offset + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + offset + 0.22);
        osc.connect(gain).connect(ctx.destination);
        osc.start(ctx.currentTime + offset);
        osc.stop(ctx.currentTime + offset + 0.24);
      });
    };
    ring();
    const timer = window.setInterval(ring, 2000);
    return () => window.clearInterval(timer);
  }, [ringing, enabled]);
  const enable = useCallback(async () => {
    const ctx = audioContext();
    await ctx?.resume();
    setEnabled(ctx?.state === "running");
  }, []);
  return { enabled, enable } as const;
}

type PlayerStatus = "idle" | "connecting" | "live" | "ended" | "unavailable" | "error";

/** Streams GET /recording into an <audio> element (Media Source Extensions). */
function useLiveRecording(sessionId: string, dashboardKey: string | null, enabled: boolean, attempt: number) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [status, setStatus] = useState<PlayerStatus>("idle");

  useEffect(() => {
    const audio = audioRef.current;
    if (!enabled || !audio) return;
    if (typeof MediaSource === "undefined" || !MediaSource.isTypeSupported(MSE_TYPE)) {
      setStatus("error");
      return;
    }
    const controller = new AbortController();
    const media = new MediaSource();
    const url = URL.createObjectURL(media);
    audio.src = url;
    setStatus("connecting");
    const onOpen = async () => {
      const buffer = media.addSourceBuffer(MSE_TYPE);
      buffer.mode = "sequence";
      const queue: Uint8Array[] = [];
      let reading = true;
      const pump = () => {
        if (buffer.updating || media.readyState !== "open") return;
        const next = queue.shift();
        if (next) buffer.appendBuffer(next as BufferSource);
        else if (!reading) {
          try {
            media.endOfStream();
          } catch {
            // already ended
          }
        }
      };
      buffer.addEventListener("updateend", pump);
      try {
        const response = await fetch(`${apiBaseUrl}/api/v1/sessions/${encodeURIComponent(sessionId)}/recording`, {
          headers: dashboardKey ? { "X-Dashboard-Key": dashboardKey } : {},
          cache: "no-store",
          signal: controller.signal,
        });
        if (response.status === 404) {
          setStatus("unavailable");
          return;
        }
        if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
        setStatus("live");
        void audio.play().catch(() => undefined);
        const reader = response.body.getReader();
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          if (value) {
            queue.push(value);
            pump();
          }
        }
        reading = false;
        pump();
        setStatus("ended");
      } catch {
        if (!controller.signal.aborted) setStatus("error");
      }
    };
    media.addEventListener("sourceopen", () => void onOpen(), { once: true });
    return () => {
      controller.abort();
      audio.removeAttribute("src");
      audio.load();
      URL.revokeObjectURL(url);
    };
  }, [sessionId, dashboardKey, enabled, attempt]);

  return { audioRef, status } as const;
}

const callCopy: Record<DispatcherCallState, string> = {
  idle: "",
  calling: "Se pornește microfonul…",
  ringing: "Se sună telefonul…",
  live: "Vorbești cu persoana",
  ended: "Apel încheiat.",
  failed: "",
};

const playerCopy: Record<PlayerStatus, string> = {
  idle: "Apasă „Ascultă”.",
  connecting: "Se conectează…",
  live: "În direct",
  ended: "Înregistrarea s-a încheiat.",
  unavailable: "Telefonul nu trimite încă sunet.",
  error: "Sunetul nu poate fi redat. Încearcă din nou.",
};

/** Hold to talk: records the dispatcher's microphone and sends the phrase to her earbuds. */
function usePushToTalk(sessionId: string, dashboardKey: string | null) {
  const [state, setState] = useState<"idle" | "recording" | "sending" | "sent" | "error">("idle");
  const [error, setError] = useState<string | null>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);

  const start = useCallback(async () => {
    if (recorder.current) return;
    try {
      stream.current ??= await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      const parts: Blob[] = [];
      const mimeType = ["audio/webm;codecs=opus", "audio/webm"].find((type) => MediaRecorder.isTypeSupported(type));
      const next = new MediaRecorder(stream.current, mimeType ? { mimeType } : undefined);
      next.ondataavailable = (event) => event.data.size && parts.push(event.data);
      next.onstop = async () => {
        recorder.current = null;
        const blob = new Blob(parts, { type: next.mimeType || "audio/webm" });
        if (blob.size < 1500) {
          setState("idle");
          return;
        }
        setState("sending");
        try {
          await api.sendOperatorMessage(sessionId, blob, dashboardKey ?? undefined);
          setState("sent");
        } catch {
          setState("error");
          setError("Mesajul nu a ajuns. Încearcă din nou.");
        }
      };
      next.start();
      recorder.current = next;
      setError(null);
      setState("recording");
    } catch {
      setState("error");
      setError("Microfonul nu este permis în acest browser.");
    }
  }, [sessionId, dashboardKey]);

  const stop = useCallback(() => {
    if (recorder.current && recorder.current.state !== "inactive") recorder.current.stop();
  }, []);

  useEffect(
    () => () => {
      stream.current?.getTracks().forEach((track) => track.stop());
    },
    [],
  );
  return { state, error, start, stop } as const;
}

export function LiveCall({
  session,
  dashboardKey,
  conversationTick,
  presentation,
}: {
  session: SessionView;
  dashboardKey: string | null;
  conversationTick: number;
  presentation: boolean;
}) {
  const alert = session.alert;
  const active = isUnfinished(session.state);
  const activeAlert = session.state === "ALERT" && alert?.status === "ACTIVE";
  const takenOver = Boolean(session.operator_connected_at);
  const [listening, setListening] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [taking, setTaking] = useState(false);
  const [takeError, setTakeError] = useState<string | null>(null);
  const [conversation, setConversation] = useState<ConversationResponse | null>(null);
  const callId = alert?.id ?? session.id;
  const heard = listening === callId || takenOver;
  const ringer = useRinger(activeAlert && !heard);
  const player = useLiveRecording(session.id, dashboardKey, heard, attempt);
  const talk = usePushToTalk(session.id, dashboardKey);
  const call = useDispatcherCall(session.id, dashboardKey, active);
  const inCall = call.state === "calling" || call.state === "ringing" || call.state === "live";

  // During the live call her voice comes through the call; the recording stays muted (no echo).
  useEffect(() => {
    const audio = player.audioRef.current;
    if (audio) audio.muted = call.state === "live";
  }, [call.state, player.audioRef]);

  useEffect(() => {
    let stop = false;
    const load = () =>
      api
        .conversation(session.id, dashboardKey ?? undefined)
        .then((answer) => !stop && setConversation(answer))
        .catch(() => !stop && setConversation(null));
    void load();
    const timer = active ? window.setInterval(load, 5000) : undefined;
    return () => {
      stop = true;
      window.clearInterval(timer);
    };
  }, [session.id, dashboardKey, conversationTick, active]);

  const listen = async () => {
    await ringer.enable();
    setListening(callId);
    setAttempt((value) => value + 1);
  };

  const takeOver = async () => {
    setTaking(true);
    setTakeError(null);
    await ringer.enable();
    setListening(callId);
    try {
      await api.takeover(session.id, dashboardKey ?? undefined);
      setAttempt((value) => value + 1);
    } catch {
      setTakeError("Apelul nu a putut fi preluat. Încearcă din nou.");
      setTaking(false);
      return;
    }
    setTaking(false);
    await call.start();
  };

  const reason = activeAlert
    ? session.alert_reason === "MISSED_CHECK_INS" ? "Nu a răspuns" : "A cerut ajutor"
    : session.state === "CHECK_IN" ? "Verificare în curs"
      : session.state === "UNCONFIRMED" ? "Așteaptă confirmarea"
        : "Sesiune în curs";

  return (
    <section className={`panel live-call ${activeAlert ? "live-call--active" : ""}`} id="call" aria-labelledby="call-head" data-testid="live-call">
      <div className="live-call-head">
        <span className={`call-icon ${activeAlert && !heard ? "call-icon--ringing" : ""}`} aria-hidden="true">
          {activeAlert ? <BellRing size={22} /> : <Headphones size={22} />}
        </span>
        <div>
          <h2 id="call-head">{!active ? "Sesiune încheiată" : takenOver ? "Apel preluat" : activeAlert ? "Apel de urgență" : "Sesiune în curs"}</h2>
          <p className="live-call-sub">
            {reason}
            {alert && <> · <span className="num">{formatClock(alert.created_at)}</span></>}
            {takenOver && <> · preluat la <span className="num">{formatClock(session.operator_connected_at)}</span></>}
          </p>
        </div>
        <div className="live-call-actions">
          {active && !heard && (
            <button type="button" className="btn btn--ghost" data-testid="listen-call" onClick={() => void listen()}>
              <Radio size={18} aria-hidden="true" /> Ascultă
            </button>
          )}
          {active && !takenOver && (
            <button type="button" className="btn btn--danger" data-testid="take-call" onClick={() => void takeOver()} disabled={taking}>
              <PhoneCall size={18} aria-hidden="true" /> {taking ? "Se preia…" : "Preia apelul"}
            </button>
          )}
          {active && takenOver && inCall && (
            <button type="button" className="btn btn--ghost" data-testid="hangup-call" onClick={() => void call.hangup()}>
              <PhoneOff size={18} aria-hidden="true" /> Închide apelul
            </button>
          )}
          {active && takenOver && !inCall && (
            <button type="button" className="btn btn--danger" data-testid="redial-call" onClick={() => void call.start()}>
              <PhoneCall size={18} aria-hidden="true" /> {call.state === "idle" ? "Sună persoana" : "Sună din nou"}
            </button>
          )}
          {activeAlert && !ringer.enabled && (
            <button type="button" className="chip-toggle" onClick={() => void ringer.enable()}>
              <BellOff size={15} aria-hidden="true" /> Activează sunetul
            </button>
          )}
        </div>
      </div>
      {takeError && <p className="notice notice--danger" role="alert">{takeError}</p>}
      <audio ref={call.audioRef} autoPlay hidden data-testid="call-remote" />
      {active && takenOver && call.state !== "idle" && (
        <p className={`call-line call-line--${call.state}`} role="status" data-testid="call-state" data-state={call.state}>
          {call.state === "live" && <span className="live-dot" aria-hidden="true" />}
          {call.state === "failed" ? call.error : callCopy[call.state]}
        </p>
      )}

      {active && takenOver && call.state !== "live" && (
        <div className="ptt" data-testid="ptt">
          <button
            type="button"
            className={`ptt-button ${talk.state === "recording" ? "ptt-button--on" : ""}`}
            data-testid="ptt-button"
            onPointerDown={(event) => {
              event.currentTarget.setPointerCapture(event.pointerId);
              void talk.start();
            }}
            onPointerUp={talk.stop}
            onPointerCancel={talk.stop}
            onKeyDown={(event) => {
              if ((event.key === " " || event.key === "Enter") && !event.repeat) {
                event.preventDefault();
                void talk.start();
              }
            }}
            onKeyUp={(event) => {
              if (event.key === " " || event.key === "Enter") talk.stop();
            }}
          >
            <Mic size={22} aria-hidden="true" />
            {talk.state === "recording" ? "Vorbește… (eliberează ca să trimiți)" : "Ține apăsat și vorbește"}
          </button>
          <p className="c-hint" role="status" data-testid="ptt-status">
            {talk.state === "sending" && "Se trimite…"}
            {talk.state === "sent" && "Mesajul a ajuns în căștile ei."}
            {talk.state === "error" && talk.error}
            {talk.state === "idle" && "Mesaj vocal: ea îl aude în căști, iar răspunsul ei îl auzi mai jos."}
          </p>
        </div>
      )}

      <div className="live-call-body">
        <div className="call-audio">
          <audio ref={player.audioRef} controls preload="auto" data-testid="call-audio" />
          <p className="c-hint" data-testid="call-audio-status" data-status={player.status}>
            {player.status === "live" && <span className="live-dot" aria-hidden="true" />} {playerCopy[player.status]}
          </p>
          {(player.status === "ended" || player.status === "error") && active && (
            <button type="button" className="text-button" onClick={() => setAttempt((value) => value + 1)}>
              <RotateCcw size={14} aria-hidden="true" /> Reconectează
            </button>
          )}
        </div>

        <div className="call-transcript" role="log" aria-live="polite" data-testid="call-transcript">
          <h3>Transcriere</h3>
          {presentation ? (
            <p className="c-hint">Ascunsă în modul prezentare.</p>
          ) : conversation && conversation.turns.length > 0 ? (
            conversation.turns.map((turn) => (
              <div key={turn.id} className={`chat-line chat-line--${turn.role}`}>
                <span className="chat-who">{turn.role === "assistant" ? "Vesta" : turn.role === "operator" ? "Dispecer" : "Persoana"}</span>
                <p>{turn.text}</p>
                <time className="num">{formatClock(turn.at)}</time>
              </div>
            ))
          ) : (
            <p className="c-hint">Încă nimic.</p>
          )}
        </div>
      </div>
    </section>
  );
}
