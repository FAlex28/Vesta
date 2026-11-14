import type { Signal } from "../state/presentation";
import type { LiveLink } from "../state/useLiveConnection";
import { linkCopy } from "../state/presentation";

/*
 * The data path to this panel, drawn as a chain: phone -> Raspberry Pi server -> live stream ->
 * this panel. A solid hop means both ends report a connection; dashed means at least one
 * does not. The joystick sits on the Pi's GPIO and is not reported separately by the server.
 */

type Mark = "on" | "off" | "warn" | "unknown";

function Station({ label, state, mark }: { label: string; state: string; mark: Mark }) {
  return (
    <div className={`station station--${mark}`} role="listitem">
      <span className="station-mark" aria-hidden="true" />
      <span className="station-text">
        <strong>{label}</strong>
        <span>{state}</span>
      </span>
    </div>
  );
}

function Hop({ on }: { on: boolean }) {
  return <span className={`hop ${on ? "hop--on" : ""}`} aria-hidden="true" />;
}

export function WatchChain({ link, serverReached, phone }: { link: LiveLink; serverReached: boolean; phone: Signal }) {
  const streamOn = link.state === "CONNECTED";
  const streamMark: Mark = streamOn ? "on" : link.state === "RECONNECTING" ? "warn" : "off";
  const serverState = streamOn || serverReached ? "Răspunde" : link.everConnected ? "Nu răspunde acum" : "Încă nu a răspuns";

  return (
    <div className="chain" role="list" aria-label="Drumul datelor până la acest panou">
      <Station label="Telefon" state={phone.state} mark={phone.mark} />
      <Hop on={phone.mark === "on" && (streamOn || serverReached)} />
      <Station label="Raspberry Pi · server" state={serverState} mark={streamOn || serverReached ? "on" : "off"} />
      <Hop on={streamOn} />
      <Station label="Flux în timp real" state={linkCopy[link.state].label} mark={streamMark} />
      <Hop on={streamOn} />
      <Station label="Acest panou" state={streamOn ? "Primește evenimente" : "Arată ultima stare cunoscută"} mark={streamOn ? "on" : "warn"} />
    </div>
  );
}
