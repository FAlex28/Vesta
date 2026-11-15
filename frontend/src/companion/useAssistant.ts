import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, newEventId } from "../api/client";
import { isUnfinished, type AssistantLine, type AssistantSpeech, type SessionView } from "../api/contract";
import { AudioEngine } from "./audioEngine";
import type { Credential } from "./useCompanionSession";
import { usePhoneCall } from "./usePhoneCall";

/*
 * Vesta in the earbuds.
 *
 * 1. The joystick starts a session; the server asks right away ("check-in"). The phone says the
 *    question ("A fost o apăsare din greșeală?") and reports it as delivered by voice.
 * 2. With Gemini configured, the phone listens for one phrase at a time and sends it to the
 *    server, which applies the intent: false alarm (stop, delete), help (alert), "I'm fine".
 *    No answer: the server's timers escalate after the second unanswered question.
 * 3. During an alert Vesta keeps talking with her and everything is recorded for the dispatcher.
 * Without Gemini the questions are spoken/shown and she answers with the on-screen buttons.
 *
 * From the first second of the session the whole conversation (her microphone + Vesta's voice)
 * is recorded and uploaded in 1 s chunks. The server shows it to the dispatcher only once an
 * alert exists and deletes it on a false alarm.
 */

export type AssistantPhase = "off" | "arming" | "ready" | "speaking" | "listening" | "thinking";
export type ChatLine = { id: string; role: "assistant" | "person" | "operator"; text: string; at: number };
type Options = {
  credential: Credential | null;
  session: SessionView | null;
  applySession: (session: SessionView) => void;
  reportDelivery: (checkinId: string, channel: "android" | "voice") => Promise<unknown> | void;
};

const LISTEN_STATES = new Set(["ACTIVE", "CHECK_IN", "UNCONFIRMED", "ALERT"]);
/** Silence after which Vesta asks something on her own (company, information for the police). */
export const NUDGE_AFTER_MS = 15_000;
const NUDGE_STATES = new Set(["ACTIVE", "ALERT"]);

const LOUD_KEY = "vesta.loud";
function loadLoud(): boolean {
  try {
    return window.localStorage.getItem(LOUD_KEY) !== "0";
  } catch {
    return true;
  }
}

export function useAssistant({ credential, session, applySession, reportDelivery }: Options) {
  const engine = useRef(new AudioEngine());
  // Loud mode (default): no echo cancellation, so Android keeps media volume instead of the much
  // quieter "call" volume. The other mode uses the earbuds' microphone through the call profile.
  const [loud, setLoudState] = useState(loadLoud);
  const [phase, setPhase] = useState<AssistantPhase>("off");
  const [armError, setArmError] = useState<string | null>(null);
  const [lines, setLines] = useState<ChatLine[]>([]);
  const [recording, setRecording] = useState<{ sessionId: string | null; chunks: number; failed: boolean }>({
    sessionId: null,
    chunks: 0,
    failed: false,
  });
  const speechChain = useRef<Promise<void>>(Promise.resolve());
  const announcedAlert = useRef(new Set<string>());
  const spokenCheckins = useRef(new Set<string>());
  const saidFalseAlarm = useRef(new Set<string>());
  const saidOperator = useRef(new Set<string>());
  const lastActivity = useRef(Date.now());
  const busy = useRef(false); // speaking or waiting for the assistant's answer
  const loopListening = useRef(false);
  const loopRun = useRef(0);
  const [soundBlocked, setSoundBlocked] = useState(false);
  const [micStalled, setMicStalled] = useState(false);
  const armed = phase !== "off" && phase !== "arming";

  const addLine = useCallback((role: ChatLine["role"], text: string) => {
    lastActivity.current = Date.now();
    setLines((current) => [...current.slice(-30), { id: newEventId("line"), role, text, at: Date.now() }]);
  }, []);

  // ------------------------------------------------------------------ arming (user gesture)

  const arm = useCallback(async () => {
    setPhase("arming");
    setArmError(null);
    try {
      await engine.current.arm({ loud: loadLoud() });
      setPhase("ready");
      return true;
    } catch (error) {
      const name = error instanceof DOMException ? error.name : "";
      setArmError(
        name === "NotAllowedError"
          ? "Microfonul nu a fost permis. Permite-l din setările browserului pentru această pagină."
          : !window.isSecureContext
            ? "Pagina nu este securizată (HTTPS), iar browserul blochează microfonul."
            : "Microfonul nu a putut porni. Verifică dacă alt program îl folosește.",
      );
      setPhase("off");
      return false;
    }
  }, []);

  const disarm = useCallback(async () => {
    await engine.current.disarm();
    setPhase("off");
  }, []);

  const setLoud = useCallback(
    async (value: boolean) => {
      try {
        window.localStorage.setItem(LOUD_KEY, value ? "1" : "0");
      } catch {
        // per-phone convenience only
      }
      setLoudState(value);
      if (engine.current.armed) {
        await engine.current.disarm();
        await arm();
      }
    },
    [arm],
  );

  // Only a real unmount (leaving the page) releases the microphone, not React's dev re-mount.
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    const audio = engine.current;
    return () => {
      mounted.current = false;
      window.setTimeout(() => {
        if (!mounted.current) void audio.disarm();
      }, 0);
    };
  }, []);

  // No touch needed after the first time: once the browser remembers the microphone permission,
  // the phone prepares Vesta on its own (on load, and again when a session starts).
  const autoArm = useCallback(async () => {
    if (engine.current.armed) return;
    try {
      const status = await navigator.permissions?.query({ name: "microphone" as PermissionName });
      if (status?.state === "granted") await arm();
    } catch {
      // Permissions API missing: the person taps "Pregătește Vesta" once.
    }
  }, [arm]);
  useEffect(() => {
    void autoArm();
  }, [autoArm]);

  // Browsers keep sound off until the first touch on a freshly loaded page; any touch unlocks it.
  useEffect(() => {
    const unlock = () => void engine.current.unlock().then(() => setSoundBlocked(armed && !engine.current.canPlay));
    window.addEventListener("pointerdown", unlock);
    window.addEventListener("keydown", unlock);
    const timer = window.setInterval(() => {
      setSoundBlocked(armed && !engine.current.canPlay);
      setMicStalled(armed && !engine.current.micAlive);
    }, 1000);
    return () => {
      window.removeEventListener("pointerdown", unlock);
      window.removeEventListener("keydown", unlock);
      window.clearInterval(timer);
    };
  }, [armed]);

  // ------------------------------------------------------------------ speaking (serialized)

  const say = useCallback(
    (speech: AssistantSpeech): Promise<void> => {
      const next = speechChain.current.then(async () => {
        addLine("assistant", speech.text);
        busy.current = true;
        setPhase("speaking");
        try {
          if (speech.audio_base64 && engine.current.armed) await engine.current.play(speech.audio_base64);
          else await engine.current.speak(speech.text);
        } catch {
          await engine.current.speak(speech.text);
        } finally {
          busy.current = false;
          lastActivity.current = Date.now();
          setPhase(!engine.current.armed ? "off" : loopListening.current ? "listening" : "ready");
        }
      });
      speechChain.current = next.catch(() => undefined);
      return next;
    },
    [addLine],
  );

  const prompt = useCallback(
    async (line: AssistantLine, eventId: string, onStart?: () => void) => {
      if (!credential) return;
      try {
        const speech = await api.assistantPrompt(credential.sessionId, credential.token, eventId, line);
        onStart?.();
        await say(speech);
      } catch {
        // The question is still on screen; the safety timers do not depend on the voice.
      }
    },
    [credential, say],
  );

  // ------------------------------------------------------------------ continuous recording

  const sessionId = credential?.sessionId ?? null;
  const takenOver = Boolean(session?.operator_connected_at);
  const takenOverRef = useRef(takenOver);
  takenOverRef.current = takenOver;
  // A session started by the button: prepare everything on the phone without a touch.
  useEffect(() => {
    if (sessionId) void autoArm();
  }, [sessionId, autoArm]);
  const unfinished = session ? isUnfinished(session.state) : false;
  useEffect(() => {
    const audio = engine.current;
    if (!armed || !sessionId || !credential || !unfinished) return;
    let seq = 0;
    let chain = Promise.resolve();
    setRecording({ sessionId, chunks: 0, failed: false });
    const upload = (chunk: Blob) => {
      const mine = seq;
      seq += 1;
      chain = chain.then(async () => {
        for (let attempt = 0; attempt < 4; attempt += 1) {
          try {
            const result = await api.recordingChunk(sessionId, credential.token, mine, chunk);
            setRecording({ sessionId, chunks: result.chunks, failed: false });
            return;
          } catch (error) {
            if (error instanceof ApiError && (error.kind === "conflict" || error.kind === "unauthorized")) return;
            await new Promise((resolve) => window.setTimeout(resolve, 500 * (attempt + 1)));
          }
        }
        setRecording((current) => ({ ...current, failed: true }));
      });
    };
    const started = audio.startRecording(upload);
    if (!started) setRecording({ sessionId, chunks: 0, failed: true });
    return () => {
      audio.stopRecording(); // the last chunk still arrives and is uploaded
    };
  }, [armed, sessionId, credential, unfinished]);

  // ------------------------------------------------------------------ conversation

  const state = session?.state ?? null;
  const checkinKey = state === "CHECK_IN" ? session?.checkin_id ?? null : null;
  const checkinRequested = state === "CHECK_IN" && session?.checkin_status === "REQUESTED";
  const listening = state !== null && LISTEN_STATES.has(state);
  const latestSession = useRef(session);
  latestSession.current = session;

  // What Vesta says on her own: the server's question, the alert, the dispatcher taking over.
  // Played between her phrases; it never interrupts the listening below.
  useEffect(() => {
    if (!credential || !sessionId || !state) return;
    // The dispatcher took the call: Vesta says so once, then stays quiet.
    if (takenOver) {
      if (!saidOperator.current.has(sessionId)) {
        saidOperator.current.add(sessionId);
        void prompt("operator", `operator-${sessionId}`);
      }
      return;
    }
    // The server's question: spoken once per check-in, then reported as delivered.
    if (checkinKey && !spokenCheckins.current.has(checkinKey)) {
      if (engine.current.armed) {
        spokenCheckins.current.add(checkinKey);
        try {
          navigator.vibrate?.([200, 100, 200]);
        } catch {
          // optional
        }
        // Delivered as soon as the question starts playing: its length must not eat the answer time.
        void prompt("checkin", `prompt-${checkinKey}`, () => {
          if (checkinRequested) void reportDelivery(checkinKey, "voice");
        });
      } else if (checkinRequested && document.visibilityState === "visible") {
        // Earbuds not prepared: the question on the open screen is what reached her.
        void reportDelivery(checkinKey, "android");
      }
    } else if (state === "ALERT" && !announcedAlert.current.has(sessionId)) {
      announcedAlert.current.add(sessionId);
      void prompt("alert", `alert-${sessionId}`);
    }
  }, [armed, takenOver, credential, sessionId, state, checkinKey, checkinRequested, prompt, reportDelivery]);

  // Listening: one loop for the whole session. A change of state on the server (question,
  // no answer, alert) must not cut off the phrase she is saying, so it is not restarted for it.
  useEffect(() => {
    if (!armed || !credential || !sessionId || !listening || takenOver) return;
    const controller = new AbortController();
    const { signal } = controller;
    loopRun.current += 1;
    const mine = loopRun.current;
    const newest = () => loopRun.current === mine;
    const pause = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms));

    const run = async () => {
      loopListening.current = true;
      while (!signal.aborted) {
        if (!busy.current) setPhase("listening");
        await engine.current.unlock();
        const startedAt = Date.now();
        const phrase = await engine.current.listen({ startTimeoutMs: 6000, signal });
        if (signal.aborted) break;
        if (!phrase || phrase.durationMs < 350) {
          if (Date.now() - startedAt < 300) await pause(500); // microphone not ready: do not spin
          continue;
        }
        lastActivity.current = Date.now();
        busy.current = true;
        setPhase("thinking");
        try {
          const reply = await api.assistantReply(sessionId, credential.token, newEventId("hear"), phrase.wavBase64);
          if (reply.transcript) addLine("person", reply.transcript);
          if (reply.action === "ESCALATE" && reply.applied) announcedAlert.current.add(sessionId);
          if (reply.action === "FALSE_ALARM" && reply.applied) saidFalseAlarm.current.add(sessionId);
          await say(reply.reply);
          applySession(reply.session);
          const current = reply.session.state;
          if (!isUnfinished(current) || !LISTEN_STATES.has(current)) break;
        } catch {
          if (newest()) setPhase(engine.current.armed ? "ready" : "off");
          await new Promise((resolve) => window.setTimeout(resolve, 1500));
        } finally {
          busy.current = false;
        }
      }
      // An older run that just stopped must not switch off the listening of the new one.
      if (!newest()) return;
      loopListening.current = false;
      if (engine.current.armed) setPhase((current) => (current === "listening" || current === "thinking" ? "ready" : current));
    };
    void run();
    return () => controller.abort();
  }, [armed, takenOver, credential, sessionId, listening, say, addLine, applySession]);

  // Every 15 s of silence (in ACTIVE or ALERT) Vesta asks something: is she fine, where is she,
  // what helps the police. The official check-ins of the server keep their own timing.
  const latestState = useRef(state);
  latestState.current = state;
  useEffect(() => {
    if (!armed || !credential || !sessionId) return;
    lastActivity.current = Date.now();
    let asking = false;
    const timer = window.setInterval(() => {
      const current = latestState.current;
      if (asking || busy.current || engine.current.hearing || takenOverRef.current || !current || !NUDGE_STATES.has(current)) return;
      if (Date.now() - lastActivity.current < NUDGE_AFTER_MS) return;
      asking = true;
      lastActivity.current = Date.now();
      void prompt("nudge", newEventId("nudge")).finally(() => {
        asking = false;
      });
    }, 1000);
    return () => window.clearInterval(timer);
  }, [armed, credential, sessionId, prompt]);

  // The dispatcher's phrases (push to talk), played in her earbuds as they arrive.
  useEffect(() => {
    if (!takenOver || !credential || !sessionId) return;
    let stop = false;
    let last: string | null = null;
    let fetching = false;
    const poll = async () => {
      if (fetching) return;
      fetching = true;
      try {
        const messages = await api.operatorMessages(sessionId, credential.token, last);
        for (const message of messages) {
          if (stop) return;
          last = message.id;
          const next = speechChain.current.then(async () => {
            addLine("operator", "Mesaj vocal de la dispecer");
            busy.current = true;
            setPhase("speaking");
            try {
              if (engine.current.armed) await engine.current.play(message.audio_base64);
            } catch {
              // a broken clip must not block the next ones
            } finally {
              busy.current = false;
              setPhase(engine.current.armed ? "ready" : "off");
            }
          });
          speechChain.current = next.catch(() => undefined);
        }
      } catch {
        // retried on the next tick
      } finally {
        fetching = false;
      }
    };
    void poll();
    const timer = window.setInterval(poll, 1500);
    return () => {
      stop = true;
      window.clearInterval(timer);
    };
  }, [takenOver, credential, sessionId, addLine]);

  // The dispatcher calls: answered by itself, they talk live.
  const ensureMic = useCallback(async () => engine.current.armed || (await arm()), [arm]);
  const call = usePhoneCall({ engine, credential, active: unfinished, ensureMic });

  // A false alarm stopped by the on-screen button is confirmed in the earbuds too.
  const cancelledFalseAlarm = session?.state === "CANCELLED" && session.cancellation_reason === "FALSE_ALARM";
  useEffect(() => {
    if (!cancelledFalseAlarm || !sessionId || saidFalseAlarm.current.has(sessionId)) return;
    saidFalseAlarm.current.add(sessionId);
    void prompt("false_alarm", `false-alarm-${sessionId}`);
  }, [cancelledFalseAlarm, sessionId, prompt]);

  // New session: new conversation on screen.
  useEffect(() => {
    if (sessionId) setLines([]);
  }, [sessionId]);

  return {
    phase,
    armed,
    arm,
    disarm,
    armError,
    lines,
    recording,
    soundBlocked,
    micStalled,
    loud,
    setLoud,
    takenOver,
    call,
    level: () => engine.current.level,
  } as const;
}
