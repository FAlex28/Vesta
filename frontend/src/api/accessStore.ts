import type { AccessScope } from "./contract";

/*
 * Device access grants (phone or trusted contact), obtained once with a pairing code.
 *
 * Kept in localStorage so an Android tab that the browser reloads in the background does not
 * need a new code. The grant only works for one device and one role, it expires (24 h by
 * default) and "Uită asocierea" removes it. Session control tokens are never stored: the phone
 * asks the server for a fresh one whenever a session runs.
 */

export type AccessGrant = { scope: AccessScope; deviceId: string; token: string; expiresAt: string };

const key = (scope: AccessScope) => `vesta.access.${scope}`;

export function loadGrant(scope: AccessScope): AccessGrant | null {
  try {
    const raw = window.localStorage.getItem(key(scope));
    if (!raw) return null;
    const grant = JSON.parse(raw) as AccessGrant;
    if (grant.scope !== scope || !grant.token || !(Date.parse(grant.expiresAt) > Date.now())) {
      window.localStorage.removeItem(key(scope));
      return null;
    }
    return grant;
  } catch {
    return null;
  }
}

export function saveGrant(grant: AccessGrant): void {
  try {
    window.localStorage.setItem(key(grant.scope), JSON.stringify(grant));
  } catch {
    // Private mode / blocked storage: the grant lives for this page only.
  }
}

export function clearGrant(scope: AccessScope): void {
  try {
    window.localStorage.removeItem(key(scope));
  } catch {
    // Nothing stored.
  }
}
