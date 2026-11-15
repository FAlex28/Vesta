import { Crosshair } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Circle, CircleMarker, MapContainer, Polyline, TileLayer, ZoomControl, useMap } from "react-leaflet";
import { latLng, type LatLngTuple } from "leaflet";
import type { SessionState } from "../api/contract";

type CityMapProps = {
  point: LatLngTuple;
  state: SessionState | null;
  /** "device": the person's live fix. "area": just the city, no marker. "simulated": a labelled test point. */
  kind: "simulated" | "device" | "area";
  accuracy?: number | null;
  /** Where she has been (oldest first), drawn behind the current position. */
  trail?: LatLngTuple[];
};

// Mirrors the status tokens in styles.css. Tiles are dark grey, so this point is the only colour.
const markerColor = (state: SessionState | null) => {
  if (state === "ALERT") return "#ff5c50";
  if (state === "CHECK_IN" || state === "UNCONFIRMED") return "#ffc23d";
  if (state === "ACTIVE") return "#5cc1e6";
  return "#c3cfd7";
};

/** Closer for a precise fix; the accuracy circle always fits on screen. */
function zoomFor(accuracy: number | null | undefined): number {
  if (accuracy == null) return 17;
  if (accuracy <= 20) return 18;
  if (accuracy <= 60) return 17;
  if (accuracy <= 200) return 16;
  return 15;
}

/**
 * Keeps the person in the centre while the position updates. Leaflet must know its real size
 * first (the map is created inside a panel that is still laying out), otherwise the point is
 * drawn off-centre. Dragging the map pauses following until "Recentrează".
 */
function FollowPoint({ point, accuracy, live }: { point: LatLngTuple; accuracy?: number | null; live: boolean }) {
  const map = useMap();
  const [following, setFollowing] = useState(true);
  const first = useRef(true);

  useEffect(() => {
    const container = map.getContainer();
    const observer = new ResizeObserver(() => {
      map.invalidateSize({ pan: false });
      if (following) map.setView(point, map.getZoom(), { animate: false });
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, [map, point, following]);

  useEffect(() => {
    map.invalidateSize({ pan: false });
    if (first.current) {
      first.current = false;
      if (accuracy != null && accuracy > 200) {
        map.fitBounds(latLng(point).toBounds(accuracy * 2.2), { animate: false });
      } else {
        map.setView(point, zoomFor(accuracy), { animate: false });
      }
      return;
    }
    if (following) map.panTo(point, { animate: true, duration: 0.6 });
  }, [map, point, accuracy, following]);

  useEffect(() => {
    if (!live) return;
    const stop = () => setFollowing(false);
    map.on("dragstart", stop);
    return () => {
      map.off("dragstart", stop);
    };
  }, [map, live]);

  if (!live || following) return null;
  return (
    <button
      type="button"
      className="map-recenter"
      onClick={() => {
        setFollowing(true);
        map.setView(point, Math.max(map.getZoom(), zoomFor(accuracy)), { animate: true });
      }}
    >
      <Crosshair size={15} aria-hidden="true" /> Recentrează
    </button>
  );
}

export function CityMap({ point, state, kind, accuracy, trail }: CityMapProps) {
  const live = kind === "device";
  return (
    <MapContainer
      key={kind}
      center={point}
      zoom={live ? zoomFor(accuracy) : kind === "area" ? 14 : 16}
      scrollWheelZoom={live}
      dragging={live}
      zoomControl={false}
      className="city-map"
    >
      <TileLayer
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        maxZoom={19}
      />
      <ZoomControl position="bottomright" />
      <FollowPoint point={point} accuracy={accuracy} live={live} />
      {live && trail && trail.length > 1 && (
        <>
          <Polyline
            positions={[...trail, point]}
            interactive={false}
            pathOptions={{ color: markerColor(state), weight: 4, opacity: 0.6, lineCap: "round", lineJoin: "round" }}
          />
          {/* Where the route started. */}
          <CircleMarker
            center={trail[0]}
            radius={5}
            interactive={false}
            pathOptions={{ color: markerColor(state), weight: 2, fillColor: "#0d1418", fillOpacity: 1 }}
          />
        </>
      )}
      {kind === "area" ? null : kind === "simulated" ? (
        // Dashed ring: marks a reference point, not a tracked position.
        <CircleMarker
          center={point}
          radius={26}
          interactive={false}
          pathOptions={{ color: "#b8a3f5", weight: 1.5, dashArray: "3 5", fill: false }}
        />
      ) : (
        accuracy != null && (
          // Reported accuracy, in metres: the person is somewhere inside this circle.
          <Circle
            center={point}
            radius={accuracy}
            interactive={false}
            pathOptions={{ color: markerColor(state), weight: 1, fillColor: markerColor(state), fillOpacity: 0.16 }}
          />
        )
      )}
      {kind !== "area" && <CircleMarker
        center={point}
        radius={9}
        interactive={false}
        pathOptions={{ color: "#ffffff", fillColor: markerColor(state), fillOpacity: 1, weight: 3 }}
      />}
    </MapContainer>
  );
}
