import { useState } from "react";
import { api, ApiError } from "../api/client";
import type { AccessScope, PairingCodeResponse } from "../api/contract";
import { formatClock } from "../state/presentation";
import { SectionHead } from "./SectionHead";

/*
 * Operator-only: one-time codes for the phone (/companion) and the trusted contact (/contact).
 * The code is shown here and typed once on the other device; it expires in minutes and works
 * once. The backend requires the dashboard key for this when one is configured.
 */

const copy: Record<AccessScope, { button: string; route: string; who: string }> = {
  companion: { button: "Cod pentru telefon", route: "/companion", who: "Pe telefonul persoanei protejate" },
  contact: { button: "Cod pentru contact", route: "/contact", who: "Pe dispozitivul contactului de încredere" },
};

export function PairingPanel({ dashboardKey, onUnauthorized }: { dashboardKey: string; onUnauthorized: () => void }) {
  const [codes, setCodes] = useState<Partial<Record<AccessScope, PairingCodeResponse>>>({});
  const [busy, setBusy] = useState<AccessScope | null>(null);
  const [error, setError] = useState<string | null>(null);

  const create = async (scope: AccessScope) => {
    setBusy(scope);
    setError(null);
    try {
      const code = await api.createPairingCode(scope, dashboardKey || undefined);
      setCodes((current) => ({ ...current, [scope]: code }));
    } catch (failure) {
      if (failure instanceof ApiError && failure.kind === "unauthorized") {
        setError("Panoul trebuie autentificat cu cheia de acces.");
        onUnauthorized();
      } else {
        setError("Codul nu a putut fi creat: serverul nu a răspuns.");
      }
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="panel area-pairing" id="pairing" aria-labelledby="pairing-head">
      <SectionHead title="Asociere" id="pairing-head" />
      <p className="panel-note">
        Telefonul se asociază o singură dată; după aceea preia singur fiecare sesiune pornită din joystick. Contactul vede
        doar alertele acestui dispozitiv.
      </p>
      {error && <p className="notice notice--warning" role="alert">{error}</p>}
      <div className="pairing-grid">
        {(Object.keys(copy) as AccessScope[]).map((scope) => {
          const code = codes[scope];
          const valid = code && Date.parse(code.expires_at) > Date.now();
          return (
            <div className="pairing-item" key={scope}>
              <button type="button" className="btn btn--outline" onClick={() => void create(scope)} disabled={busy !== null} data-testid={`pair-code-${scope}`}>
                {busy === scope ? "Se creează…" : copy[scope].button}
              </button>
              {code && (
                <div className="pairing-code" data-testid={`pair-code-value-${scope}`}>
                  <span className="pairing-code-value num" aria-label="Cod de asociere">
                    {code.code.slice(0, 4)} {code.code.slice(4)}
                  </span>
                  <span className="pairing-code-help">
                    {valid
                      ? `${copy[scope].who}, deschide ${window.location.origin}${copy[scope].route} și introdu codul. Valabil până la ${formatClock(code.expires_at)}, o singură dată.`
                      : "Codul a expirat. Creează altul."}
                  </span>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}
