#!/usr/bin/env python3
"""Server HTTP de test pentru camera termică USB-C (UVC) + camera normală pe Raspberry Pi.

Nu folosește aplicația producătorului: camera termică este citită direct prin V4L2/UVC.
Majoritatea modulelor termice USB-C pentru Android (senzor 256x192) apar în Linux ca o
cameră UVC care trimite cadre YUYV 256x384: jumătatea de sus = imaginea, jumătatea de jos =
date brute de temperatură pe 16 biți (Kelvin * 64).

Rute:
  /                         pagina de test
  /stream/thermal.mjpg      stream termic (?palette=inferno|turbo|jet|hot|gray)
  /stream/rgb.mjpg          stream cameră normală
  /snapshot/thermal.jpg     cadru termic
  /snapshot/rgb.jpg         cadru cameră normală
  /api/status               stare camere + statistici temperatură (JSON)
  /api/thermal/point?x=&y=  temperatura într-un punct (x, y normalizate 0..1)
  /api/thermal/matrix.json  matricea de temperaturi (?step=N pentru subeșantionare)
  /api/thermal/raw.npy      matricea de temperaturi float32 (numpy)
"""

from __future__ import annotations

import argparse
import glob
import hmac
import io
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent

# Noduri video interne ale Raspberry Pi (codec, ISP, CSI) - nu sunt camere USB.
INTERNAL_NAME_HINTS = ("bcm2835", "unicam", "rpivid", "pispbe", "rp1-cfe", "hevc", "codec", "isp")
# Lățimi tipice pentru senzori termici; înălțimea senzorului = lățime * 3/4.
THERMAL_WIDTHS = (160, 240, 256, 384)
MAX_META_ROWS = 8


def sensor_height(w: int) -> int:
    return w * 3 // 4


def thermal_size(w: int, h: int) -> bool:
    """256x192 (doar imagine), 256x194 (imagine+metadate), 256x384/386 (imagine+temperatură)."""
    if w not in THERMAL_WIDTHS:
        return False
    sh = sensor_height(w)
    return sh <= h <= sh + MAX_META_ROWS or 2 * sh <= h <= 2 * sh + MAX_META_ROWS
PALETTES = {
    "inferno": cv2.COLORMAP_INFERNO,
    "turbo": getattr(cv2, "COLORMAP_TURBO", cv2.COLORMAP_JET),
    "jet": cv2.COLORMAP_JET,
    "hot": cv2.COLORMAP_HOT,
    "gray": None,
}


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, flush=True)


# ---------------------------------------------------------------------------
# Descoperire dispozitive
# ---------------------------------------------------------------------------

def list_formats(dev: str) -> list[tuple[str, int, int]]:
    try:
        out = subprocess.run(
            ["v4l2-ctl", "-d", dev, "--list-formats-ext"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    fmts, cur = [], None
    for line in out.splitlines():
        m = re.search(r"\[\d+\]: '(\w+)'", line)
        if m:
            cur = m.group(1)
            continue
        m = re.search(r"Size: Discrete (\d+)x(\d+)", line)
        if m and cur:
            fmts.append((cur, int(m.group(1)), int(m.group(2))))
    return fmts


def video_devices() -> list[dict]:
    devs = []
    for path in sorted(glob.glob("/dev/video*"), key=lambda p: int(re.sub(r"\D", "", p) or 0)):
        n = path.replace("/dev/video", "")
        try:
            name = Path(f"/sys/class/video4linux/video{n}/name").read_text().strip()
        except OSError:
            name = "?"
        if any(h in name.lower() for h in INTERNAL_NAME_HINTS):
            continue
        fmts = list_formats(path)
        if not fmts:  # nod de metadate sau fără capturare
            continue
        devs.append({"path": path, "name": name, "formats": fmts})
    return devs


def is_thermal(dev: dict) -> bool:
    name = dev["name"].lower()
    if any(k in name for k in ("thermal", "infiray", "tiny1", "ircam", "p2pro", "tc001", "vantrue")):
        return True
    return any(thermal_size(w, h) for _, w, h in dev["formats"])


def pick_thermal_mode(dev: dict) -> tuple[str, int, int]:
    # Preferăm modul cu cele mai multe rânduri (imagine + temperatură).
    yuyv = [f for f in dev["formats"] if f[0] == "YUYV" and thermal_size(f[1], f[2])]
    if yuyv:
        return max(yuyv, key=lambda f: f[2])
    return dev["formats"][0]


_PICAM_CACHE: dict = {}


def picamera_available() -> bool:
    """Verificare izolată: pe unele Pi-uri fără Pi Camera, importul picamera2 se blochează."""
    if "ok" not in _PICAM_CACHE:
        code = "from picamera2 import Picamera2; import sys; sys.exit(0 if Picamera2.global_camera_info() else 1)"
        try:
            r = subprocess.run(["python3", "-c", code], capture_output=True, timeout=10)
            _PICAM_CACHE["ok"] = r.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            _PICAM_CACHE["ok"] = False
    return _PICAM_CACHE["ok"]


# ---------------------------------------------------------------------------
# Camere
# ---------------------------------------------------------------------------

class FrameSource:
    """Firul de citire păstrează ultimul cadru; clienții așteaptă cadre noi."""

    def __init__(self, label: str):
        self.label = label
        self.cond = threading.Condition()
        self.frame_id = 0
        self.payload = None
        self.connected = False
        self.device = None
        self.mode = None
        self.error = None
        self.fps = 0.0
        self._t_last = None

    def publish(self, payload):
        now = time.monotonic()
        if self._t_last is not None:
            dt = now - self._t_last
            if dt > 0:
                self.fps = self.fps * 0.9 + (1.0 / dt) * 0.1 if self.fps else 1.0 / dt
        self._t_last = now
        with self.cond:
            self.payload = payload
            self.frame_id += 1
            self.cond.notify_all()

    def wait_new(self, last_id: int, timeout: float = 1.0):
        with self.cond:
            if self.frame_id == last_id:
                self.cond.wait(timeout)
            return self.frame_id, self.payload

    def mark_down(self, err: str):
        self.connected = False
        self.error = err
        self.fps = 0.0
        self._t_last = None


class ThermalCamera(FrameSource):
    def __init__(self, device: str | None, temp_divisor: float, swap_halves: bool):
        super().__init__("thermal")
        self.forced_device = device
        self.temp_divisor = temp_divisor
        self.swap_halves = swap_halves
        self._render_cache: dict = {}
        self._render_lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    def _find(self):
        devs = video_devices()
        if self.forced_device:
            for d in devs:
                if d["path"] == self.forced_device:
                    return d
            return {"path": self.forced_device, "name": "?", "formats": []}
        for d in devs:
            if is_thermal(d):
                return d
        return None

    def _loop(self):
        while True:
            dev = self._find()
            if not dev:
                self.mark_down("Camera termică nu a fost găsită (verifică USB și `v4l2-ctl --list-devices`).")
                time.sleep(2)
                continue
            try:
                self._run(dev)
            except Exception as exc:  # noqa: BLE001
                log("termic: eroare", exc)
                self.mark_down(str(exc))
            time.sleep(2)

    def _run(self, dev):
        fourcc, w, h = pick_thermal_mode(dev) if dev["formats"] else ("YUYV", 256, 384)
        idx = int(re.sub(r"\D", "", dev["path"]))
        cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
        if not cap.isOpened():
            raise RuntimeError(f"Nu pot deschide {dev['path']}")
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        if fourcc == "YUYV":
            cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)  # vrem octeții bruți, nu BGR
        self.device = f"{dev['path']} ({dev['name']})"
        log(f"termic: {self.device} {fourcc} {w}x{h}")
        fails = 0
        try:
            while True:
                ok, frame = cap.read()
                if not ok or frame is None:
                    fails += 1
                    if fails > 30:
                        raise RuntimeError("Camera termică nu mai trimite cadre")
                    time.sleep(0.05)
                    continue
                fails = 0
                data = self._decode(frame, fourcc, w, h)
                self.connected, self.error = True, None
                self.mode = data["mode"]
                self.publish(data)
        finally:
            cap.release()

    def _decode(self, frame, fourcc, w, h):
        buf = np.asarray(frame)
        if fourcc == "YUYV" and buf.size == w * h * 2:
            raw = buf.reshape(h, w, 2)
            sh = sensor_height(w)
            if h >= 2 * sh:
                # Imagine sus (sh rânduri), eventual rânduri de metadate, temperaturi în ultimele sh rânduri.
                top, bottom = raw[:sh], raw[h - sh :]
                if self.swap_halves:
                    top, bottom = raw[h - sh :], raw[:sh]
                luma = top[..., 0]
                raw16 = bottom[..., 0].astype(np.uint16) | (bottom[..., 1].astype(np.uint16) << 8)
                temps = raw16.astype(np.float32) / self.temp_divisor - 273.15
                med = float(np.median(temps))
                if -40.0 < med < 200.0:
                    return {"mode": "radiometric", "luma": luma, "temps": temps, "raw16": raw16}
                return {"mode": "doar imagine (date temperatură invalide)", "luma": luma, "temps": None,
                        "raw16": raw16}
            return {"mode": "doar imagine (fără date de temperatură)", "luma": raw[: min(h, sh), :, 0],
                    "temps": None}
        # Fallback: OpenCV a convertit deja la BGR sau format necunoscut.
        if buf.ndim == 3 and buf.shape[2] == 3:
            gray = cv2.cvtColor(buf, cv2.COLOR_BGR2GRAY)
        elif buf.ndim == 3 and buf.shape[2] == 2:
            gray = buf[..., 0]
        else:
            gray = buf.reshape(-1)[: w * h].reshape(h, w) if buf.size >= w * h else np.zeros((h, w), np.uint8)
        return {"mode": "doar imagine (format neașteptat)", "luma": gray, "temps": None}

    # --- statistici / redare ---

    def render(self, frame_id, payload, palette: str, scale: int = 3) -> bytes:
        key = (frame_id, palette)
        with self._render_lock:
            if key in self._render_cache:
                return self._render_cache[key]
        src = payload["temps"] if payload["temps"] is not None else payload["luma"].astype(np.float32)
        # Percentile: un singur punct foarte fierbinte nu mai înnegrește restul imaginii.
        lo, hi = (float(v) for v in np.percentile(src, (1.0, 99.5)))
        norm = ((src - lo) / max(hi - lo, 1e-3) * 255.0).clip(0, 255).astype(np.uint8)
        norm = cv2.equalizeHist(norm)  # contrast automat (doar vizual; temperaturile rămân exacte)
        cmap = PALETTES.get(palette, cv2.COLORMAP_INFERNO)
        img = cv2.cvtColor(norm, cv2.COLOR_GRAY2BGR) if cmap is None else cv2.applyColorMap(norm, cmap)
        hh, ww = norm.shape
        img = cv2.resize(img, (ww * scale, hh * scale), interpolation=cv2.INTER_CUBIC)
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        data = jpg.tobytes()
        with self._render_lock:
            if len(self._render_cache) > 16:
                self._render_cache.clear()
            self._render_cache[key] = data
        return data

class DeviceBusy(RuntimeError):
    pass


def device_busy(path: str) -> bool:
    try:
        r = subprocess.run(["v4l2-ctl", "-d", path, "--stream-mmap", "--stream-count=1"],
                           capture_output=True, text=True, timeout=5)
        return "busy" in (r.stdout + r.stderr).lower()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


class RgbCamera(FrameSource):
    def __init__(self, source: str, size: tuple[int, int], exclude: ThermalCamera, fallback_url: str | None):
        super().__init__("rgb")
        self.source = source  # "auto", "picam", /dev/videoN sau http://.../flux.mjpg
        self.size = size
        self.exclude = exclude
        self.fallback_url = fallback_url
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                if self.source.startswith("http"):
                    self._run_http(self.source)
                    continue
                dev = None if self.source == "picam" else self._find_usb()
                if dev:
                    try:
                        self._run_usb(dev)
                    except DeviceBusy:
                        if not self.fallback_url:
                            raise
                        log(f"rgb: {dev['path']} e ocupat, folosesc fluxul {self.fallback_url}")
                        self._run_http(self.fallback_url)
                elif self.source in ("picam", "auto") and picamera_available():
                    self._run_picam()
                else:
                    self.mark_down("Nicio cameră normală găsită (webcam USB sau Pi Camera).")
            except Exception as exc:  # noqa: BLE001
                log("rgb: eroare", exc)
                self.mark_down(str(exc))
            time.sleep(2)

    def _run_http(self, url: str):
        """Citește un flux MJPEG (multipart/x-mixed-replace) și republică cadrele JPEG neschimbate."""
        import urllib.request

        with urllib.request.urlopen(url, timeout=10) as resp:
            self.device = f"flux MJPEG {url} (webcam folosit de alt program)"
            buf, first = b"", True
            while True:
                chunk = resp.read(32768)
                if not chunk:
                    raise RuntimeError("Fluxul MJPEG s-a închis")
                buf += chunk
                while True:
                    s = buf.find(b"\xff\xd8")
                    if s < 0:
                        buf = buf[-1:]
                        break
                    e = buf.find(b"\xff\xd9", s + 2)
                    if e < 0:
                        buf = buf[s:]
                        break
                    jpg, buf = buf[s : e + 2], buf[e + 2 :]
                    if first:
                        img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                        if img is not None:
                            self.mode = f"{img.shape[1]}x{img.shape[0]}"
                            first = False
                    self.connected, self.error = True, None
                    self.publish(jpg)

    def _find_usb(self):
        thermal_path = (self.exclude.device or "").split(" ")[0]
        for d in video_devices():
            if self.source not in ("auto", d["path"]):
                continue
            if d["path"] == thermal_path or (self.source == "auto" and is_thermal(d)):
                continue
            return d
        return None

    def _publish_bgr(self, bgr):
        ok, jpg = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            self.connected, self.error = True, None
            self.publish(jpg.tobytes())

    def _run_picam(self):
        from picamera2 import Picamera2

        cam = Picamera2()
        cfg = cam.create_video_configuration(main={"size": self.size, "format": "RGB888"})
        cam.configure(cfg)
        cam.start()
        self.device, self.mode = "Raspberry Pi Camera (libcamera)", f"{self.size[0]}x{self.size[1]}"
        log("rgb:", self.device)
        try:
            while True:
                self._publish_bgr(cam.capture_array("main"))  # RGB888 din picamera2 = ordine BGR
        finally:
            cam.stop()
            cam.close()

    def _run_usb(self, dev):
        idx = int(re.sub(r"\D", "", dev["path"]))
        cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
        if not cap.isOpened():
            if device_busy(dev["path"]):
                raise DeviceBusy(dev["path"])
            raise RuntimeError(f"Nu pot deschide {dev['path']}")
        if any(f[0] == "MJPG" for f in dev["formats"]):
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.size[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.size[1])
        self.device = f"{dev['path']} ({dev['name']})"
        self.mode = f"{int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}"
        log("rgb:", self.device, self.mode)
        fails = 0
        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    fails += 1
                    if fails > 30:
                        raise RuntimeError("Camera normală nu mai trimite cadre")
                    time.sleep(0.05)
                    continue
                fails = 0
                self._publish_bgr(frame)
        finally:
            cap.release()


def placeholder(text: str) -> bytes:
    img = np.full((384, 512, 3), 32, np.uint8)
    cv2.putText(img, text, (20, 192), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 200, 200), 1, cv2.LINE_AA)
    return cv2.imencode(".jpg", img)[1].tobytes()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    thermal: ThermalCamera
    rgb: RgbCamera
    api_key: str | None = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # liniște în consolă
        pass

    def _send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        p = url.path
        if p.startswith(("/api/", "/stream/", "/snapshot/")) and self.api_key:
            supplied = self.headers.get("X-Vesta-Camera-Key", "")
            if not hmac.compare_digest(self.api_key, supplied):
                self._json({"error": "unauthorized"}, code=401)
                return
        if p in ("/", "/index.html") and self.api_key:
            self._json({"error": "not_found"}, code=404)
            return
        try:
            if p in ("/", "/index.html"):
                self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif p == "/stream/thermal.mjpg":
                self._stream(self.thermal, q.get("palette", "inferno"))
            elif p == "/stream/rgb.mjpg":
                self._stream(self.rgb, None)
            elif p == "/snapshot/thermal.jpg":
                self._send(200, self._jpeg(self.thermal, q.get("palette", "inferno")), "image/jpeg")
            elif p == "/snapshot/rgb.jpg":
                self._send(200, self._jpeg(self.rgb, None), "image/jpeg")
            elif p == "/api/status":
                self._json(self._status())
            elif p == "/api/thermal/point":
                self._point(q)
            elif p == "/api/thermal/matrix.json":
                self._matrix(q)
            elif p == "/api/thermal/raw.npy":
                temps = self._temps()
                if temps is None:
                    return self._json({"error": "Nu există date de temperatură"}, 503)
                bio = io.BytesIO()
                np.save(bio, temps.astype(np.float32))
                self._send(200, bio.getvalue(), "application/octet-stream")
            else:
                self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # --- helpers ---

    def _temps(self):
        payload = self.thermal.payload
        return None if not payload else payload["temps"]

    def _jpeg(self, cam: FrameSource, palette):
        fid, payload = cam.frame_id, cam.payload
        if not cam.connected or payload is None:
            return placeholder(("Camera termica" if cam is self.thermal else "Camera normala") + " indisponibila")
        return self.thermal.render(fid, payload, palette) if cam is self.thermal else payload

    def _stream(self, cam: FrameSource, palette):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        last = -1
        while True:
            fid, _ = cam.wait_new(last, timeout=1.0)
            if fid == last and cam.connected:
                continue
            last = fid
            jpg = self._jpeg(cam, palette)
            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                             + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
            if not cam.connected:
                time.sleep(1)

    def _status(self):
        def base(cam: FrameSource):
            return {"connected": cam.connected, "device": cam.device, "mode": cam.mode,
                    "fps": round(cam.fps, 1), "error": cam.error}
        return {"thermal": base(self.thermal), "rgb": base(self.rgb), "time": time.time()}

    def _point(self, q):
        temps = self._temps()
        if temps is None:
            return self._json({"error": "Nu există date de temperatură"}, 503)
        h, w = temps.shape
        try:
            x = min(w - 1, max(0, int(float(q["x"]) * w)))
            y = min(h - 1, max(0, int(float(q["y"]) * h)))
        except (KeyError, ValueError):
            return self._json({"error": "Parametri x, y (0..1) lipsă"}, 400)
        self._json({"x": x, "y": y, "temp_c": round(float(temps[y, x]), 2)})

    def _matrix(self, q):
        temps = self._temps()
        if temps is None:
            return self._json({"error": "Nu există date de temperatură"}, 503)
        step = max(1, int(q.get("step", 1)))
        sub = np.round(temps[::step, ::step].astype(np.float64), 1)
        self._json({"unit": "C", "step": step, "width": sub.shape[1], "height": sub.shape[0],
                    "data": sub.tolist()})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.getenv("VESTA_CAMERA_BIND_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--thermal", default=None, help="forțează dispozitivul termic, ex. /dev/video0")
    ap.add_argument("--rgb", default="auto", help="auto | picam | /dev/videoN | http://.../flux.mjpg")
    ap.add_argument("--rgb-fallback", default="http://127.0.0.1:5000/video_feed",
                    help="flux MJPEG folosit dacă webcam-ul e ocupat de alt program ('' = dezactivat)")
    ap.add_argument("--rgb-size", default="640x480")
    ap.add_argument("--temp-divisor", type=float, default=64.0, help="raw16 / divizor = Kelvin (implicit 64)")
    ap.add_argument("--swap-halves", action="store_true", help="datele de temperatură sunt în jumătatea de sus")
    ap.add_argument("--list", action="store_true", help="afișează camerele găsite și ieși")
    args = ap.parse_args()

    api_key = (os.getenv("VESTA_CAMERA_API_KEY") or "").strip()
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not api_key:
        ap.error("VESTA_CAMERA_API_KEY is required when binding outside loopback")
    Handler.api_key = api_key or None

    if args.list:
        for d in video_devices():
            kind = "TERMIC" if is_thermal(d) else "normal"
            sizes = sorted({f"{f[0]} {f[1]}x{f[2]}" for f in d["formats"]})
            print(f"{d['path']}  [{kind}]  {d['name']}\n    " + "\n    ".join(sizes))
        print("Pi Camera (libcamera):", "da" if picamera_available() else "nu")
        return

    w, h = (int(v) for v in args.rgb_size.lower().split("x"))
    Handler.thermal = ThermalCamera(args.thermal, args.temp_divisor, args.swap_halves)
    Handler.rgb = RgbCamera(args.rgb, (w, h), Handler.thermal, args.rgb_fallback or None)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    log(f"Server pornit pe {args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
