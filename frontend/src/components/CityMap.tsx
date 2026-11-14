import { Circle, CircleMarker, MapContainer, TileLayer, ZoomControl } from "react-leaflet";
import type { LatLngTuple } from "leaflet";
import type { SessionState } from "../api/contract";

type CityMapProps = {
  point: LatLngTuple;
  state: SessionState | null;
  /** "simulated": dashed reference ring around a fixed test point. "device": phone's own fix. */
  kind: "simulated" | "device";
  accuracy?: number | null;
};

// Mirrors the status tokens in styles.css. Tiles are greyscale, so this point is the only colour.
const markerColor = (state: SessionState | null) => {
  if (state === "ALERT") return "#b8321f";
  if (state === "ACTIVE" || state === "CHECK_IN" || state === "UNCONFIRMED") return "#2b5f75";
  return "#14202b";
};

export function CityMap({ point, state, kind, accuracy }: CityMapProps) {
  return (
    <MapContainer
      key={`${point[0]},${point[1]}`}
      center={point}
      zoom={16}
      scrollWheelZoom={false}
      dragging={false}
      zoomControl={false}
      className="city-map"
    >
      <TileLayer
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
      />
      <ZoomControl position="bottomright" />
      {kind === "simulated" ? (
        // Dashed ring: marks a reference point, not a tracked position.
        <CircleMarker
          center={point}
          radius={26}
          interactive={false}
          pathOptions={{ color: "#14202b", weight: 1.5, dashArray: "3 5", fill: false }}
        />
      ) : (
        accuracy != null && (
          // Reported accuracy, in metres.
          <Circle
            center={point}
            radius={accuracy}
            interactive={false}
            pathOptions={{ color: "#2b5f75", weight: 1, fillColor: "#2b5f75", fillOpacity: 0.12 }}
          />
        )
      )}
      <CircleMarker
        center={point}
        radius={8}
        interactive={false}
        pathOptions={{ color: "#fff", fillColor: markerColor(state), fillOpacity: 1, weight: 3 }}
      />
    </MapContainer>
  );
}
