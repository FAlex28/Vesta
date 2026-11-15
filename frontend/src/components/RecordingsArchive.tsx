import { Pause, Play } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { apiBaseUrl } from "../api/config";
import type { ArchivedRecording, ConversationTurn } from "../api/contract";
import { alertReasonLabel, formatClock, resolutionLabel } from "../state/presentation";
import { SectionHead } from "./SectionHead";

/* Every alert's recording, kept in the database: listen and read the transcript any time. */

function formatDate(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return `${date.toLocaleDateString("ro-RO", { day: "2-digit", month: "2-digit" })} ${formatClock(value).slice(0, 5)}`;
}

function minutes(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function Row({ item, dashboardKey, presentation }: { item: ArchivedRecording; dashboardKey: string | null; presentation: boolean }) {
  const [url, setUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const [turns, setTurns] = useState<ConversationTurn[] | null>(null);
  const audio = useRef<HTMLAudioElement | null>(null);

  useEffect(() => () => {
    if (url) URL.revokeObjectURL(url);
  }, [url]);

  const load = async () => {
    if (url) {
      if (audio.current?.paused) void audio.current.play();
      else audio.current?.pause();
      return;
    }
    setLoading(true);
    setFailed(false);
    try {
      const response = await fetch(`${apiBaseUrl}/api/v1/sessions/${encodeURIComponent(item.session_id)}/recording`, {
        headers: dashboardKey ? { "X-Dashboard-Key": dashboardKey } : {},
        cache: "no-store",
      });
      if (!response.ok) throw new Error(String(response.status));
      const blob = await response.blob();
      const next = URL.createObjectURL(new Blob([blob], { type: "audio/webm" }));
      setUrl(next);
      window.setTimeout(() => void audio.current?.play(), 0);
    } catch {
      setFailed(true);
    } finally {
      setLoading(false);
    }
  };

  const showTranscript = async () => {
    if (turns) {
      setTurns(null);
      return;
    }
    try {
      const answer = await api.conversation(item.session_id, dashboardKey ?? undefined);
      setTurns(answer.turns);
    } catch {
      setTurns([]);
    }
  };

  const resolution = item.resolution ? resolutionLabel(item.resolution) : item.finalized ? "închisă" : "în desfășurare";

  return (
    <li className="rec-row" data-testid="rec-row">
      <button type="button" className="rec-play" onClick={() => void load()} disabled={item.chunks === 0 || loading} aria-label="Ascultă înregistrarea">
        {url && !audio.current?.paused ? <Pause size={18} aria-hidden="true" /> : <Play size={18} aria-hidden="true" />}
      </button>
      <div className="rec-main">
        <p className="rec-title">
          <strong>{alertReasonLabel(item.alert_reason)}</strong>
          <span className="num">{formatDate(item.alert_at ?? item.started_at)}</span>
        </p>
        <p className="rec-meta">
          <span className="num">{item.chunks ? minutes(item.chunks) : "fără sunet"}</span>
          {resolution && <> · {resolution}</>}
          {item.operator_at && <> · preluat de dispecer</>}
          {item.turns > 0 && (
            <>
              {" · "}
              <button type="button" className="text-button" onClick={() => void showTranscript()}>
                {turns ? "Ascunde transcrierea" : `Transcriere (${item.turns})`}
              </button>
            </>
          )}
        </p>
        {loading && <p className="c-hint">Se încarcă…</p>}
        {failed && <p className="c-hint">Înregistrarea nu a putut fi încărcată.</p>}
        {url && <audio ref={audio} src={url} controls preload="auto" className="rec-audio" />}
        {turns && (
          <div className="rec-transcript">
            {presentation ? (
              <p className="c-hint">Ascunsă în modul prezentare.</p>
            ) : (
              turns.map((turn) => (
                <p key={turn.id}>
                  <span className="chat-who">
                    {turn.role === "assistant" ? "Vesta" : turn.role === "operator" ? "Dispecer" : "Persoana"}
                  </span>{" "}
                  {turn.text} <time className="num" dateTime={turn.at}>{formatClock(turn.at)}</time>
                </p>
              ))
            )}
          </div>
        )}
      </div>
    </li>
  );
}

export function RecordingsArchive({ dashboardKey, presentation, refreshKey }: { dashboardKey: string | null; presentation: boolean; refreshKey: unknown }) {
  const [items, setItems] = useState<ArchivedRecording[] | null>(null);

  useEffect(() => {
    let stop = false;
    const load = () =>
      api
        .recordingsArchive(dashboardKey ?? undefined)
        .then((answer) => !stop && setItems(answer))
        .catch(() => !stop && setItems((current) => current ?? []));
    void load();
    const timer = window.setInterval(load, 15_000);
    return () => {
      stop = true;
      window.clearInterval(timer);
    };
  }, [dashboardKey, refreshKey]);

  return (
    <section className="panel" id="recordings" aria-labelledby="recordings-head">
      <SectionHead title="Înregistrări" id="recordings-head">
        <span className="panel-meta">{items ? `${items.length}` : ""}</span>
      </SectionHead>
      {items === null ? (
        <p className="c-hint">Se încarcă…</p>
      ) : items.length === 0 ? (
        <p className="c-hint">Nicio înregistrare.</p>
      ) : (
        <ul className="rec-list">
          {items.map((item) => (
            <Row key={item.session_id} item={item} dashboardKey={dashboardKey} presentation={presentation} />
          ))}
        </ul>
      )}
    </section>
  );
}
