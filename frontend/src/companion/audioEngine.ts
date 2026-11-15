/*
 * Audio on the person's phone, through her Bluetooth earbuds:
 *
 * - one microphone stream (the earbuds' mic when Android routes it there),
 * - a continuous recording of the whole conversation: her microphone mixed with Vesta's voice,
 *   uploaded in 1 s chunks (MediaRecorder, audio/webm),
 * - detection of one spoken phrase (simple voice-activity detection) encoded as 16 kHz WAV,
 *   which is what Gemini accepts,
 * - playback of Vesta's voice (ElevenLabs mp3, or the browser voice when there is no key).
 *
 * Everything starts only after an explicit tap ("Pregătește Vesta"): the browser requires a user
 * gesture for the microphone and for sound. A silent tone keeps the audio session alive so the
 * page is not frozen in the background.
 */

const TARGET_RATE = 16_000;
const FRAME = 4096;

export type ListenOptions = {
  /** Give up when nobody starts speaking within this time. */
  startTimeoutMs: number;
  /** Silence after speech that ends the phrase. */
  endSilenceMs?: number;
  /** Hard limit for one phrase. */
  maxMs?: number;
  signal?: AbortSignal;
};

export type Phrase = { wavBase64: string; durationMs: number };

export class AudioEngine {
  private ctx: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private mix: MediaStreamAudioDestinationNode | null = null;
  private processor: ScriptProcessorNode | null = null;
  private keepAlive: OscillatorNode | null = null;
  private recorder: MediaRecorder | null = null;
  private frameListener: ((frame: Float32Array) => void) | null = null;
  private wakeLock: { release: () => Promise<void> } | null = null;
  private noiseFloor = 0.01;
  private output: AudioNode | null = null;
  /** While Vesta speaks, the microphone is not interpreted (no answer to her own voice). */
  private speakingNow = false;
  level = 0;

  get armed(): boolean {
    return this.ctx !== null && this.stream !== null;
  }

  /** False until the browser lets this page play sound (it needs one touch after loading). */
  get canPlay(): boolean {
    return this.ctx?.state === "running";
  }

  /** Call from any touch on the page: unlocks sound if the browser held it back. */
  async unlock(): Promise<void> {
    await this.ctx?.resume().catch(() => undefined);
  }

  get sampleRate(): number {
    return this.ctx?.sampleRate ?? 48_000;
  }

  /** Must run inside a user gesture. */
  async arm({ loud = true }: { loud?: boolean } = {}): Promise<void> {
    if (this.armed) {
      await this.ctx?.resume();
      return;
    }
    // loud: no voice processing, so Android does not switch to the quiet "call" audio mode.
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: loud
        ? { echoCancellation: false, noiseSuppression: false, autoGainControl: false }
        : { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    const ctx = new AudioContext();
    // Without a touch the browser may keep it suspended; unlock() resumes it on the next touch.
    void ctx.resume().catch(() => undefined);
    const source = ctx.createMediaStreamSource(stream);
    const mix = ctx.createMediaStreamDestination();
    source.connect(mix);

    const processor = ctx.createScriptProcessor(FRAME, 1, 1);
    const mute = ctx.createGain();
    mute.gain.value = 0;
    source.connect(processor);
    processor.connect(mute);
    mute.connect(ctx.destination);
    processor.onaudioprocess = (event) => {
      const frame = event.inputBuffer.getChannelData(0);
      const rms = Math.sqrt(frame.reduce((sum, value) => sum + value * value, 0) / frame.length);
      this.level = rms;
      this.frameListener?.(new Float32Array(frame));
    };

    // Inaudible tone: Android keeps a page that plays audio running when the screen is off.
    const keepAlive = ctx.createOscillator();
    const silent = ctx.createGain();
    silent.gain.value = 0.0001;
    keepAlive.frequency.value = 20;
    keepAlive.connect(silent);
    silent.connect(ctx.destination);
    keepAlive.start();

    // Vesta's voice: louder (gain) but without distortion (compressor/limiter).
    const boost = ctx.createGain();
    boost.gain.value = 3.2;
    const limiter = ctx.createDynamicsCompressor();
    limiter.threshold.value = -10;
    limiter.knee.value = 6;
    limiter.ratio.value = 12;
    limiter.attack.value = 0.003;
    limiter.release.value = 0.15;
    boost.connect(limiter);
    limiter.connect(ctx.destination);
    limiter.connect(mix);

    this.output = boost;
    this.ctx = ctx;
    this.stream = stream;
    this.mix = mix;
    this.processor = processor;
    this.keepAlive = keepAlive;
    await this.holdWakeLock();
  }

  private async holdWakeLock(): Promise<void> {
    try {
      const nav = navigator as Navigator & { wakeLock?: { request: (type: "screen") => Promise<{ release: () => Promise<void> }> } };
      this.wakeLock = (await nav.wakeLock?.request("screen")) ?? null;
    } catch {
      this.wakeLock = null; // optional
    }
  }

  async disarm(): Promise<void> {
    this.stopRecording();
    this.frameListener = null;
    this.processor?.disconnect();
    try {
      this.keepAlive?.stop();
    } catch {
      // already stopped
    }
    this.stream?.getTracks().forEach((track) => track.stop());
    await this.wakeLock?.release().catch(() => undefined);
    await this.ctx?.close().catch(() => undefined);
    this.ctx = null;
    this.stream = null;
    this.mix = null;
    this.processor = null;
    this.keepAlive = null;
    this.wakeLock = null;
    this.output = null;
  }

  // ------------------------------------------------------------------ recording

  startRecording(onChunk: (chunk: Blob) => void): boolean {
    if (!this.mix || this.recorder) return Boolean(this.recorder);
    const mimeType = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"].find((type) =>
      typeof MediaRecorder !== "undefined" && MediaRecorder.isTypeSupported(type),
    );
    if (!mimeType) return false;
    const recorder = new MediaRecorder(this.mix.stream, { mimeType, audioBitsPerSecond: 32_000 });
    recorder.ondataavailable = (event) => {
      if (event.data.size > 0) onChunk(event.data);
    };
    recorder.start(1000);
    this.recorder = recorder;
    return true;
  }

  stopRecording(): void {
    if (this.recorder && this.recorder.state !== "inactive") this.recorder.stop();
    this.recorder = null;
  }

  get recording(): boolean {
    return this.recorder !== null;
  }

  // ------------------------------------------------------------------ Vesta speaks

  /** ElevenLabs mp3: heard in the earbuds and mixed into the recording. */
  async play(audioBase64: string): Promise<void> {
    const ctx = this.ctx;
    if (!ctx) throw new Error("audio not armed");
    const bytes = Uint8Array.from(atob(audioBase64), (char) => char.charCodeAt(0));
    const buffer = await ctx.decodeAudioData(bytes.buffer);
    this.speakingNow = true;
    try {
      await new Promise<void>((resolve) => {
        const source = ctx.createBufferSource();
        source.buffer = buffer;
        source.connect(this.output ?? ctx.destination);
        source.onended = () => resolve();
        source.start();
      });
    } finally {
      this.speakingNow = false;
    }
  }

  /** Browser voice (no ElevenLabs key). Heard, but not part of the recording. */
  speak(text: string): Promise<void> {
    return new Promise((resolve) => {
      if (!("speechSynthesis" in window)) {
        resolve();
        return;
      }
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.lang = "ro-RO";
      utterance.volume = 1;
      utterance.rate = 0.95;
      const voice = window.speechSynthesis.getVoices().find((item) => item.lang.toLowerCase().startsWith("ro"));
      if (voice) utterance.voice = voice;
      this.speakingNow = true;
      const done = () => {
        window.clearTimeout(guard);
        this.speakingNow = false;
        resolve();
      };
      const guard = window.setTimeout(done, Math.min(20_000, 1500 + text.length * 90));
      utterance.onend = done;
      utterance.onerror = done;
      window.speechSynthesis.cancel();
      window.speechSynthesis.speak(utterance);
    });
  }

  // ------------------------------------------------------------------ she speaks

  /** Resolves with one phrase, or null when nobody spoke before `startTimeoutMs`. */
  listen({ startTimeoutMs, endSilenceMs = 1200, maxMs = 15_000, signal }: ListenOptions): Promise<Phrase | null> {
    const rate = this.sampleRate;
    return new Promise((resolve) => {
      if (!this.armed) {
        resolve(null);
        return;
      }
      const frameMs = (FRAME / rate) * 1000;
      const captured: Float32Array[] = [];
      let previous: Float32Array | null = null;
      let speaking = false;
      let speechFrames = 0;
      let silenceMs = 0;
      let elapsedMs = 0;
      const finish = (result: Phrase | null) => {
        this.frameListener = null;
        signal?.removeEventListener("abort", onAbort);
        resolve(result);
      };
      const onAbort = () => finish(null);
      signal?.addEventListener("abort", onAbort);

      this.frameListener = (frame) => {
        if (this.speakingNow) {
          // Vesta's own voice: not an answer. Start over once she is quiet.
          if (!speaking) elapsedMs = 0;
          return;
        }
        elapsedMs += frameMs;
        const rms = Math.sqrt(frame.reduce((sum, value) => sum + value * value, 0) / frame.length);
        const threshold = Math.max(0.012, this.noiseFloor * 3);
        const loud = rms > threshold;
        if (!speaking) {
          // Slowly learn the background noise while nobody speaks.
          this.noiseFloor = this.noiseFloor * 0.95 + rms * 0.05;
          speechFrames = loud ? speechFrames + 1 : 0;
          if (previous) captured.splice(0, captured.length, previous);
          captured.push(frame);
          if (speechFrames >= 2) {
            speaking = true;
            silenceMs = 0;
            elapsedMs = 0;
          } else if (elapsedMs > startTimeoutMs) {
            finish(null);
          }
          previous = frame;
          return;
        }
        captured.push(frame);
        silenceMs = loud ? 0 : silenceMs + frameMs;
        if (silenceMs >= endSilenceMs || elapsedMs >= maxMs) {
          const samples = concat(captured);
          finish({ wavBase64: encodeWav(downsample(samples, rate, TARGET_RATE), TARGET_RATE), durationMs: (samples.length / rate) * 1000 });
        }
      };
    });
  }
}

function concat(frames: Float32Array[]): Float32Array {
  const total = frames.reduce((sum, frame) => sum + frame.length, 0);
  const out = new Float32Array(total);
  let offset = 0;
  for (const frame of frames) {
    out.set(frame, offset);
    offset += frame.length;
  }
  return out;
}

export function downsample(samples: Float32Array, from: number, to: number): Float32Array {
  if (from <= to) return samples;
  const ratio = from / to;
  const length = Math.floor(samples.length / ratio);
  const out = new Float32Array(length);
  for (let i = 0; i < length; i += 1) {
    const start = Math.floor(i * ratio);
    const end = Math.min(samples.length, Math.floor((i + 1) * ratio));
    let sum = 0;
    for (let j = start; j < end; j += 1) sum += samples[j];
    out[i] = sum / Math.max(1, end - start);
  }
  return out;
}

export function encodeWav(samples: Float32Array, rate: number): string {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const write = (offset: number, text: string) => [...text].forEach((char, i) => view.setUint8(offset + i, char.charCodeAt(0)));
  write(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  write(8, "WAVE");
  write(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  write(36, "data");
  view.setUint32(40, samples.length * 2, true);
  samples.forEach((value, i) => view.setInt16(44 + i * 2, Math.max(-1, Math.min(1, value)) * 0x7fff, true));
  let binary = "";
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}
