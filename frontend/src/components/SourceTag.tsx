import type { DataSource } from "../types";

const defaultLabel: Record<DataSource, string> = {
  simulated: "Date simulate",
  live: "Live",
  unavailable: "Indisponibil",
};

/**
 * Provenance marker. Hatched square = test/simulated, solid = live, hollow = unavailable.
 * The same hatch underlines individual test values, so the shape carries the meaning
 * together with the text, never colour alone.
 */
export function SourceTag({ source, label }: { source: DataSource; label?: string }) {
  return (
    <span className={`src src--${source}`}>
      <span className="src-mark" aria-hidden="true" />
      {label ?? defaultLabel[source]}
    </span>
  );
}
