import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";

/*
 * Web Push for the emergency contact: one tap on "Permite notificările" asks the browser's
 * permission, registers /sw.js, subscribes with the device's public VAPID key and sends the
 * subscription to the server. Nothing happens without that tap.
 */

export type PushState = "unsupported" | "insecure" | "denied" | "off" | "working" | "on" | "failed";

function keyBytes(base64url: string): Uint8Array<ArrayBuffer> {
  const padded = base64url.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (base64url.length % 4)) % 4);
  const raw = window.atob(padded);
  const bytes = new Uint8Array(new ArrayBuffer(raw.length));
  for (let index = 0; index < raw.length; index += 1) bytes[index] = raw.charCodeAt(index);
  return bytes;
}

const supported = () => typeof window !== "undefined" && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

export function usePush(token: string | null, serverSaysOn: boolean | null) {
  const [state, setState] = useState<PushState>(() => {
    if (!supported()) return "unsupported";
    if (!window.isSecureContext) return "insecure";
    if (Notification.permission === "denied") return "denied";
    return "off";
  });

  // Reflect what the server knows (e.g. after a reload or on another tab).
  useEffect(() => {
    if (serverSaysOn === null) return;
    setState((current) => (current === "working" || current === "unsupported" || current === "insecure" ? current : serverSaysOn ? "on" : current === "on" ? "off" : current));
  }, [serverSaysOn]);

  const enable = useCallback(async () => {
    if (!token || !supported() || !window.isSecureContext) return;
    setState("working");
    try {
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        setState(permission === "denied" ? "denied" : "off");
        return;
      }
      const registration = await navigator.serviceWorker.register("/sw.js");
      await navigator.serviceWorker.ready;
      const { public_key: publicKey } = await api.contactPushKey(token);
      const existing = await registration.pushManager.getSubscription();
      const subscription =
        existing ?? (await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(publicKey) }));
      await api.contactPushSubscribe(token, subscription.toJSON());
      setState("on");
    } catch {
      setState("failed");
    }
  }, [token]);

  const disable = useCallback(async () => {
    if (!token || !supported()) return;
    setState("working");
    try {
      const registration = await navigator.serviceWorker.getRegistration("/sw.js");
      const subscription = await registration?.pushManager.getSubscription();
      if (subscription) {
        await api.contactPushUnsubscribe(token, subscription.endpoint);
        await subscription.unsubscribe();
      }
      setState("off");
    } catch {
      setState("failed");
    }
  }, [token]);

  return { state, enable, disable } as const;
}
