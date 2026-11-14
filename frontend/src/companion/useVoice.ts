import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import { CONTRACT_TECHNICAL_ISSUES, type ContractTechnicalIssue, type VoiceAccessResponse, type VoiceStatus } from "../api/contract";
import type { Credential } from "./useCompanionSession";

/*
 * Voice conversation for the companion page.
 *
 * - Starts only from an explicit tap. The microphone permission is requested at that moment,
 *   never on page load and never because the joystick was pressed.
 * - At most one conversation exists: a second start is ignored while one is starting or open.
 * - In live mode the backend returns a short-lived ElevenLabs conversation token; audio goes
 *   phone <-> ElevenLabs over WebRTC. The token is not stored.
 * - "Connected" means the SDK reported a connection. It does not mean the person heard
 *   anything: agent text is shown as text, labelled as sent, not as heard.
 * - In the backend's scripted mode there is no ElevenLabs call and no audio; the scripted
 *   lines are shown as text and labelled as such.
 * - Every outcome is reported through POST /voice/status. The backend allows one conversation
 *   per session and keeps it reserved until it hears CONNECTED, DISCONNECTED or an error; without
 *   these reports a second start would be refused with VOICE_ALREADY_ACTIVE.
 */

export type VoicePhase =
  | "off"
  | "requesting_mic"
  | "mic_error"
  | "requesting_access"
  | "connecting"
  | "connected"
  | "disconnected"
  | "playback_error"
  | "unavailable"
  | "scripted"
  | "ended";

export type VoiceState = {
  phase: VoicePhase;
  mode: "speaking" | "listening" | null;
  detail: string | null;
  lastAgentText: { text: string; at: number } | null;
  scriptedLines: string[];
  devicesChangedAt: number | null;
};

type ToolHandlers = {
  confirmSafe: () => Promise<boolean>;
  requestHelp: () => Promise<boolean>;
  reportIssue: (issue: ContractTechnicalIssue, active: boolean) => Promise<void>;
  reportVoiceStatus: (status: VoiceStatus, conversationId: string | null, options?: { keepalive?: boolean }) => Promise<void>;
};

/** The subset of the ElevenLabs conversation object this page uses. */
type ConversationHandle = {
  endSession: () => Promise<void>;
  sendContextualUpdate: (text: string) => void;
};

type StartOptions = {
  conversationToken: string;
  connectionType: "webrtc";
  clientTools: Record<string, (parameters: Record<string, unknown>) => Promise<string> | string>;
  onConnect: (props: { conversationId: string }) => void;
  onDisconnect: (details: { reason: "error" | "agent" | "user"; message?: string }) => void;
  onError: (message: string, context?: unknown) => void;
  onModeChange: (prop: { mode: "speaking" | "listening" }) => void;
  onMessage: (props: { message: string; role?: string; source?: string }) => void;
};

type StartSession = (options: StartOptions) => Promise<ConversationHandle>;

declare global {
  interface Window {
    /** Test-only replacement for the ElevenLabs SDK. Honoured only in Vite's "test" mode. */
    __vestaVoiceFactory?: StartSession;
  }
}

async function loadStartSession(): Promise<StartSession> {
  if (import.meta.env.MODE === "test" && window.__vestaVoiceFactory) return window.__vestaVoiceFactory;
  const sdk = await import("@elevenlabs/client");
  return (options) => sdk.Conversation.startSession(options as never) as unknown as Promise<ConversationHandle>;
}

const initial: VoiceState = {
  phase: "off",
  mode: null,
  detail: null,
  lastAgentText: null,
  scriptedLines: [],
  devicesChangedAt: null,
};

function micErrorDetail(error: unknown): string {
  const name = error instanceof DOMException ? error.name : "";
  if (name === "NotAllowedError" || name === "SecurityError")
    return "Accesul la microfon a fost refuzat. Îl poți permite din setările site-ului în browser.";
  if (name === "NotFoundError") return "Nu a fost găsit niciun microfon. Verifică dacă sunt conectate căștile.";
  if (name === "NotReadableError") return "Microfonul este folosit de altă aplicație sau nu poate fi pornit.";
  return "Microfonul nu a putut fi pornit.";
}

export function useVoice(credential: Credential | null, voiceMode: "elevenlabs" | "demo" | null, tools: ToolHandlers) {
  const [state, setState] = useState<VoiceState>(initial);
  const conversation = useRef<ConversationHandle | null>(null);
  const starting = useRef(false);
  const userEnded = useRef(false);
  /** conversation_id issued by the backend for the open conversation, and whether its end was reported. */
  const conversationId = useRef<string | null>(null);
  const endReported = useRef(true);
  const toolsRef = useRef(tools);
  toolsRef.current = tools;

  const patch = useCallback((next: Partial<VoiceState>) => setState((current) => ({ ...current, ...next })), []);

  /** Report the end of the current conversation once, whichever path ends it. */
  const reportEnd = useCallback((status: VoiceStatus, options?: { keepalive?: boolean }) => {
    if (endReported.current) return;
    endReported.current = true;
    void toolsRef.current.reportVoiceStatus(status, conversationId.current, options);
  }, []);

  const start = useCallback(async () => {
    if (!credential || starting.current || conversation.current) return;
    starting.current = true;
    userEnded.current = false;
    try {
      if (!window.isSecureContext) {
        patch({
          phase: "mic_error",
          detail: "Browserul blochează microfonul pe o conexiune nesecurizată. Deschide Vesta de la adresa securizată.",
        });
        return;
      }

      // Live voice needs the microphone; ask for it now, on the user's tap.
      if (voiceMode !== "demo") {
        patch({ phase: "requesting_mic", detail: null });
        try {
          const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
          stream.getTracks().forEach((track) => track.stop()); // the SDK opens its own stream
        } catch (error) {
          patch({ phase: "mic_error", detail: micErrorDetail(error) });
          void toolsRef.current.reportVoiceStatus("MICROPHONE_ERROR", null);
          return;
        }
      }

      patch({ phase: "requesting_access", detail: null });
      let access: VoiceAccessResponse;
      try {
        access = await api.voiceAccess(credential.sessionId, credential.token);
      } catch (error) {
        // Provider failures are recorded by the backend itself; nothing to report here.
        const failure = error instanceof ApiError ? error : null;
        patch({
          phase: "unavailable",
          detail:
            failure?.kind === "network"
              ? "Serverul Vesta nu a răspuns la cererea pentru conversație."
              : failure?.code === "VOICE_ALREADY_ACTIVE"
                ? "Serverul are deja o conversație deschisă pentru această sesiune (poate în altă filă). Încearcă din nou peste câteva secunde."
                : failure?.code === "VOICE_RETRY_LATER"
                  ? "Serviciul vocal a eșuat recent. Încearcă din nou peste câteva secunde."
                  : "Conversația vocală este momentan indisponibilă.",
        });
        return;
      }
      conversationId.current = access.conversation_id;
      endReported.current = false;

      if (access.mode === "demo" || access.transport === "scripted" || !access.conversation_token) {
        // Scripted mode: the backend marks this conversation CONNECTED itself.
        endReported.current = true;
        const lines = Array.isArray(access.client_config.demo_messages) ? access.client_config.demo_messages : [];
        patch({ phase: "scripted", scriptedLines: lines, detail: null });
        return;
      }

      patch({ phase: "connecting" });
      const startSession = await loadStartSession();
      const handle = await startSession({
        conversationToken: access.conversation_token,
        connectionType: "webrtc",
        clientTools: {
          vesta_confirm_safe: async () =>
            (await toolsRef.current.confirmSafe()) ? "Confirmare înregistrată de server." : "Confirmarea nu a fost acceptată de server.",
          vesta_request_help: async () =>
            (await toolsRef.current.requestHelp()) ? "Alertă confirmată de server." : "Alerta NU a fost confirmată de server.",
          vesta_report_technical_issue: async (parameters) => {
            // The model's argument is untrusted: only contract issue codes are forwarded.
            const issue = parameters.issue;
            if (typeof issue !== "string" || !(CONTRACT_TECHNICAL_ISSUES as readonly string[]).includes(issue)) {
              return "Problema nu a fost raportată: cod necunoscut.";
            }
            await toolsRef.current.reportIssue(issue as ContractTechnicalIssue, true);
            return "Problema tehnică a fost trimisă serverului.";
          },
        },
        onConnect: () => {
          patch({ phase: "connected", detail: null });
          void toolsRef.current.reportVoiceStatus("CONNECTED", conversationId.current);
        },
        onDisconnect: (details) => {
          conversation.current = null;
          reportEnd("DISCONNECTED");
          if (userEnded.current || details.reason === "user") {
            patch({ phase: "ended", mode: null });
            return;
          }
          patch({
            phase: "disconnected",
            mode: null,
            detail: details.reason === "agent" ? "Asistentul a încheiat conversația." : "Conexiunea audio s-a întrerupt.",
          });
        },
        onError: (message) => {
          setState((current) =>
            current.phase === "connected"
              ? { ...current, phase: "playback_error", detail: `Eroare audio raportată de serviciul vocal: ${message}` }
              : current,
          );
        },
        onModeChange: ({ mode }) => patch({ mode }),
        onMessage: ({ message, role, source }) => {
          if ((role ?? source) === "ai" || role === "agent") patch({ lastAgentText: { text: message, at: Date.now() } });
        },
      });
      conversation.current = handle;
    } catch {
      conversation.current = null;
      patch({ phase: "unavailable", detail: "Conexiunea cu serviciul vocal nu a putut fi pornită." });
      reportEnd("SERVICE_UNAVAILABLE");
    } finally {
      starting.current = false;
    }
  }, [credential, voiceMode, patch, reportEnd]);

  const stop = useCallback(async () => {
    userEnded.current = true;
    const handle = conversation.current;
    conversation.current = null;
    if (handle) {
      try {
        await handle.endSession();
      } catch {
        // Already closed.
      }
    }
    reportEnd("DISCONNECTED");
    patch({ phase: "ended", mode: null, detail: null });
  }, [patch, reportEnd]);

  /** Tell the agent that the backend asked for a safety check, so it asks out loud. */
  const notifyCheckIn = useCallback(() => {
    try {
      conversation.current?.sendContextualUpdate("safety_check_requested");
    } catch {
      // A closed conversation simply cannot be told; the visual prompt still shows.
    }
  }, []);

  // Bluetooth headsets connecting/disconnecting change the device list; the OS picks the route.
  useEffect(() => {
    const media = navigator.mediaDevices;
    if (!media?.addEventListener) return;
    const onChange = () => patch({ devicesChangedAt: Date.now() });
    media.addEventListener("devicechange", onChange);
    return () => media.removeEventListener("devicechange", onChange);
  }, [patch]);

  // Losing the session credential ends the conversation.
  useEffect(() => {
    if (!credential && conversation.current) void stop();
  }, [credential, stop]);

  // Closing or leaving the page: tell the server so the next visit can start a conversation.
  useEffect(() => {
    const onPageHide = () => {
      if (!conversation.current) return;
      userEnded.current = true;
      reportEnd("DISCONNECTED", { keepalive: true });
    };
    window.addEventListener("pagehide", onPageHide);
    return () => window.removeEventListener("pagehide", onPageHide);
  }, [reportEnd]);

  useEffect(
    () => () => {
      userEnded.current = true;
      void conversation.current?.endSession().catch(() => undefined);
      conversation.current = null;
    },
    [],
  );

  const busy = state.phase === "requesting_mic" || state.phase === "requesting_access" || state.phase === "connecting";
  const open = state.phase === "connected" || state.phase === "playback_error";
  return { voice: state, start, stop, notifyCheckIn, busy, open } as const;
}
