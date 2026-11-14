"""Heartbeat de la pagina /companion. Se ține doar starea curentă per telefon; în istoric
ajung numai tranzițiile (deconectare, reconectare, pagină ascunsă/vizibilă).

Limită reală: un browser care a suspendat pagina (ecran blocat) nu mai trimite heartbeaturi,
deci serverul vede RECONNECTING/DISCONNECTED. Asta NU e interpretat ca SOS sau agresiune."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from .events import as_uuid, insert_technical_event

Visibility = Literal["VISIBLE", "HIDDEN", "UNKNOWN"]


class CompanionsRepo:
    def __init__(self, pool, settings):
        self._pool = pool
        self._s = settings

    def heartbeat(
        self, client_id: str, owner_id: str, *, session_id=None, visibility: Visibility = "UNKNOWN"
    ) -> dict:
        """Întoarce {client, previous_state, reconnected, visibility_changed}."""
        sid = as_uuid(session_id)
        with self._pool.transaction() as tx:
            prev = tx.one(
                "SELECT * FROM companion_clients WHERE client_id = %s FOR UPDATE", (client_id,)
            )
            if prev is None:
                client = tx.one(
                    "INSERT INTO companion_clients (client_id, owner_id, session_id, visibility, last_heartbeat_at) "
                    "VALUES (%s, %s, %s, %s, now()) RETURNING *",
                    (client_id, owner_id, sid, visibility),
                )
                insert_technical_event(
                    tx,
                    component="PHONE",
                    event_type="PHONE_CONNECTED",
                    session_id=sid,
                    client_id=client_id,
                )
                return {
                    "client": client,
                    "previous_state": None,
                    "reconnected": False,
                    "visibility_changed": False,
                }

            reconnected = prev["connection_state"] != "CONNECTED"
            client = tx.one(
                "UPDATE companion_clients SET connection_state = 'CONNECTED', last_heartbeat_at = now(), "
                "owner_id = %s, session_id = COALESCE(%s::uuid, session_id), visibility = %s, "
                "last_reconnect_at = CASE WHEN %s THEN now() ELSE last_reconnect_at END, "
                "disconnected_at = CASE WHEN %s THEN NULL ELSE disconnected_at END, updated_at = now() "
                "WHERE client_id = %s RETURNING *",
                (owner_id, sid, visibility, reconnected, reconnected, client_id),
            )
            if reconnected:
                insert_technical_event(
                    tx,
                    component="PHONE",
                    event_type="PHONE_RECONNECTED",
                    session_id=client["session_id"],
                    client_id=client_id,
                    metadata={
                        "previous_state": prev["connection_state"],
                        "offline_since": prev["disconnected_at"] or prev["last_heartbeat_at"],
                    },
                )
            vis_changed = visibility != "UNKNOWN" and visibility != prev["visibility"]
            if vis_changed:
                insert_technical_event(
                    tx,
                    component="PAGE",
                    event_type=f"PAGE_{visibility}",
                    session_id=client["session_id"],
                    client_id=client_id,
                )
            return {
                "client": client,
                "previous_state": prev["connection_state"],
                "reconnected": reconnected,
                "visibility_changed": vis_changed,
            }

    def sweep(self, at: datetime | None = None) -> list[dict]:
        """Apelat periodic pe server. CONNECTED → RECONNECTING după 2 heartbeaturi ratate,
        → DISCONNECTED după pragul configurat. Întoarce tranzițiile (pentru dashboard)."""
        with self._pool.transaction() as tx:
            rows = tx.all(
                """
                WITH t AS (SELECT COALESCE(%s::timestamptz, now()) AS now),
                cand AS (
                    SELECT c.client_id, c.connection_state AS previous_state,
                           CASE WHEN c.last_heartbeat_at < t.now - make_interval(secs => %s::double precision)
                                THEN 'DISCONNECTED' ELSE 'RECONNECTING' END AS new_state
                    FROM companion_clients c, t
                    WHERE c.connection_state <> 'DISCONNECTED'
                      AND c.last_heartbeat_at < t.now - make_interval(secs => %s::double precision)
                    FOR UPDATE OF c
                )
                UPDATE companion_clients c SET connection_state = cand.new_state, updated_at = now(),
                    disconnected_at = CASE WHEN cand.new_state = 'DISCONNECTED'
                                           THEN (SELECT now FROM t) ELSE c.disconnected_at END
                FROM cand
                WHERE c.client_id = cand.client_id AND cand.new_state <> cand.previous_state
                RETURNING c.*, cand.previous_state
                """,
                (at, self._s.heartbeat_disconnect_s, self._s.heartbeat_reconnecting_s),
            )
            for r in rows:
                insert_technical_event(
                    tx,
                    component="PHONE",
                    event_type="PHONE_DISCONNECTED"
                    if r["connection_state"] == "DISCONNECTED"
                    else "PHONE_HEARTBEAT_MISSED",
                    session_id=r["session_id"],
                    client_id=r["client_id"],
                    occurred_at=at,
                    metadata={
                        "last_heartbeat_at": r["last_heartbeat_at"],
                        "previous_state": r["previous_state"],
                    },
                )
            return rows

    def get(self, client_id: str) -> dict | None:
        with self._pool.autocommit() as tx:
            return tx.one("SELECT * FROM companion_clients WHERE client_id = %s", (client_id,))

    def for_owner(self, owner_id: str) -> list[dict]:
        with self._pool.autocommit() as tx:
            return tx.all(
                "SELECT * FROM companion_clients WHERE owner_id = %s ORDER BY last_heartbeat_at DESC",
                (owner_id,),
            )
