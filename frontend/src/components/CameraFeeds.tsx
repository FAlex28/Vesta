import { Camera, VideoOff } from "lucide-react";
import { useEffect, useState } from "react";
import { apiBaseUrl } from "../api/config";
import { api } from "../api/client";
import type { CameraFeedStatus, CamerasStatus } from "../api/contract";
import { SectionHead } from "./SectionHead";

type CameraKind = "rgb" | "thermal";

function marker(bytes: Uint8Array, first: number, second: number, from: number): number {
  for (let index = Math.max(0, from); index + 1 < bytes.length; index += 1) {
    if (bytes[index] === first && bytes[index + 1] === second) return index;
  }
  return -1;
}

function useCameraFrame(kind: CameraKind, dashboardKey: string, enabled: boolean) {
  const [frameUrl, setFrameUrl] = useState<string | null>(null);
  const [streamError, setStreamError] = useState(false);

  useEffect(() => {
    if (!enabled) {
      setFrameUrl(null);
      setStreamError(false);
      return;
    }

    const controller = new AbortController();
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
    let currentUrl: string | null = null;

    const consume = async () => {
      try {
        const response = await fetch(`${apiBaseUrl}/api/v1/cameras/${kind}/stream`, {
          headers: {
            Accept: "multipart/x-mixed-replace",
            ...(dashboardKey ? { "X-Dashboard-Key": dashboardKey } : {}),
          },
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok || !response.body) throw new Error("Camera stream unavailable");

        reader = response.body.getReader();
        let buffer = new Uint8Array();
        while (!controller.signal.aborted) {
          const result = await reader.read();
          if (result.done) break;
          const joined = new Uint8Array(buffer.length + result.value.length);
          joined.set(buffer);
          joined.set(result.value, buffer.length);
          buffer = joined;

          while (true) {
            const start = marker(buffer, 0xff, 0xd8, 0);
            if (start < 0) {
              buffer = buffer.slice(Math.max(0, buffer.length - 1));
              break;
            }
            const end = marker(buffer, 0xff, 0xd9, start + 2);
            if (end < 0) {
              buffer = buffer.slice(start);
              break;
            }

            const frame = buffer.slice(start, end + 2);
            buffer = buffer.slice(end + 2);
            const nextUrl = URL.createObjectURL(new Blob([frame], { type: "image/jpeg" }));
            if (currentUrl) URL.revokeObjectURL(currentUrl);
            currentUrl = nextUrl;
            setFrameUrl(nextUrl);
            setStreamError(false);
          }

          if (buffer.length > 4_000_000) buffer = buffer.slice(-2);
        }
      } catch {
        if (!controller.signal.aborted) setStreamError(true);
      }
    };

    void consume();
    return () => {
      controller.abort();
      void reader?.cancel();
      if (currentUrl) URL.revokeObjectURL(currentUrl);
    };
  }, [dashboardKey, enabled, kind]);

  return { frameUrl, streamError };
}

function CameraPanel({
  kind,
  title,
  status,
  serverReachable,
  dashboardKey,
}: {
  kind: CameraKind;
  title: string;
  status: CameraFeedStatus | null;
  serverReachable: boolean;
  dashboardKey: string;
}) {
  const connected = Boolean(serverReachable && status?.connected);
  const { frameUrl, streamError } = useCameraFrame(kind, dashboardKey, connected);
  const label = connected ? "Flux activ" : "Indisponibil";
  return (
    <section className="panel camera-panel" aria-labelledby={`camera-${kind}-title`}>
      <SectionHead title={title} id={`camera-${kind}-title`}>
        <span className={`src ${connected ? "src--live" : "src--unavailable"}`}>
          <span className="src-mark" aria-hidden="true" />
          {label}
        </span>
      </SectionHead>
      <div className={`camera-screen ${connected ? "" : "camera-screen--offline"}`} data-testid={`camera-${kind}`}>
        {frameUrl && connected ? (
          <img src={frameUrl} alt={`${title}, flux video în timp real`} />
        ) : (
          <div className="camera-empty" role="status">
            {connected ? <Camera size={28} aria-hidden="true" /> : <VideoOff size={28} aria-hidden="true" />}
            <span>
              {!serverReachable
                ? "Camerele nu răspund."
                : status?.connected
                  ? streamError
                    ? "Fluxul video nu poate fi încărcat."
                    : "Se conectează fluxul video…"
                  : "Camera nu trimite cadre video."}
            </span>
          </div>
        )}
      </div>
      <div className="camera-meta">
        <span>{connected ? status?.mode ?? "Semnal video" : "Fără semnal"}</span>
        {connected && typeof status?.fps === "number" && (
          <span className="num">{status.fps.toFixed(1)} cadre/s</span>
        )}
      </div>
    </section>
  );
}

export function CameraFeeds({ dashboardKey }: { dashboardKey: string }) {
  const [status, setStatus] = useState<CamerasStatus | null>(null);
  const [authError, setAuthError] = useState(false);

  useEffect(() => {
    let disposed = false;
    const refresh = async () => {
      try {
        const result = await api.camerasStatus(dashboardKey || undefined);
        if (!disposed) {
          setStatus(result);
          setAuthError(false);
        }
      } catch (error) {
        if (!disposed) {
          setStatus(null);
          setAuthError(Boolean(error && typeof error === "object" && "status" in error && error.status === 401));
        }
      }
    };

    void refresh();
    const timer = window.setInterval(refresh, 5_000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [dashboardKey]);

  const serverReachable = Boolean(status?.reachable);

  return (
    <section id="cameras" className="camera-grid" aria-label="Fluxuri video">
      {authError && <p className="camera-auth-hint" role="status">Autentifică panoul pentru a vedea fluxurile video.</p>}
      <CameraPanel
        kind="rgb"
        title="Camera normală"
        status={status?.rgb ?? null}
        serverReachable={serverReachable}
        dashboardKey={dashboardKey}
      />
      <CameraPanel
        kind="thermal"
        title="Camera termică"
        status={status?.thermal ?? null}
        serverReachable={serverReachable}
        dashboardKey={dashboardKey}
      />
    </section>
  );
}
