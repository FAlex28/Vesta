"""Locații de la pagina companion. Starea CURRENT/STALE/UNAVAILABLE se calculează la citire,
astfel încât o poziție veche nu e niciodată prezentată drept actuală."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from .errors import InvalidLocation
from .events import as_uuid

MAX_FUTURE_SKEW = timedelta(minutes=5)


def location_status(row: dict | None, now: datetime, settings) -> dict:
    if row is None:
        return {"status": "UNAVAILABLE"}
    age = (now - row["occurred_at"]).total_seconds()
    acc = row.get("accuracy_m")
    return {
        "status": "CURRENT" if age <= settings.gps_stale_s else "STALE",
        "lat": row["lat"], "lon": row["lon"], "accuracy_m": acc,
        "low_accuracy": acc is None or acc > settings.gps_low_accuracy_m,
        "occurred_at": row["occurred_at"], "age_s": round(max(age, 0.0), 1),
        "simulated": row["simulated"],
    }


class LocationsRepo:
    def __init__(self, pool, settings):
        self._pool = pool
        self._s = settings

    def record(
        self, *, lat: float, lon: float, session_id=None, client_id: str | None = None,
        accuracy_m: float | None = None, occurred_at: datetime | None = None, simulated: bool = False,
    ) -> dict:
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            raise InvalidLocation("lat/lon trebuie să fie numere")
        if not (math.isfinite(lat) and math.isfinite(lon)) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise InvalidLocation("lat/lon în afara intervalului")
        if accuracy_m is not None:
            accuracy_m = float(accuracy_m)
            if not math.isfinite(accuracy_m) or accuracy_m < 0:
                raise InvalidLocation("accuracy_m invalid")
        if occurred_at is not None:
            if occurred_at.tzinfo is None:
                raise InvalidLocation("occurred_at trebuie să aibă fus orar")
            if occurred_at > datetime.now(timezone.utc) + MAX_FUTURE_SKEW:
                raise InvalidLocation("occurred_at e în viitor")
        with self._pool.autocommit() as tx:
            return tx.one(
                "INSERT INTO locations (occurred_at, session_id, client_id, lat, lon, accuracy_m, source, simulated) "
                "VALUES (COALESCE(%s::timestamptz, now()), %s, %s, %s, %s, %s::real, %s, %s) RETURNING *",
                (occurred_at, as_uuid(session_id), client_id, lat, lon, accuracy_m,
                 "DEMO" if simulated else "COMPANION_GPS", simulated),
            )

    def latest(self, session_id, *, at: datetime | None = None) -> dict:
        """Ultima locație cu stare și vechime. Nu inventează coordonate: fără date → UNAVAILABLE."""
        with self._pool.autocommit() as tx:
            row = tx.one(
                "SELECT occurred_at, lat, lon, accuracy_m, simulated FROM locations "
                "WHERE session_id = %s ORDER BY occurred_at DESC LIMIT 1",
                (as_uuid(session_id),),
            )
            now = at or tx.one("SELECT now() AS n")["n"]
        return location_status(row, now, self._s)

    def set_retention(self, days: float) -> None:
        with self._pool.autocommit() as tx:
            tx.exec("SELECT remove_retention_policy('locations', if_exists => true)")
            tx.exec("SELECT add_retention_policy('locations', make_interval(secs => %s::double precision))",
                    (days * 86400,))
