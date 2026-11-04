import { addEventListener, callable, removeEventListener } from "@decky/api";
import { useEffect, useRef, useState } from "react";

/** What the plugin backend sends as the `sendspin_card` event (swe/specs/architecture/sendspin-metadata-controller-artwork.md). */
export interface CardState {
  active: boolean;
  title?: string;
  artist?: string;
  album?: string;
  /** Id of the current cover; changes whenever a new image becomes current */
  art?: string | null;
  playing?: boolean;
  position_ms?: number | null;
  duration_ms?: number | null;
  /** Device wall-clock seconds at which position_ms was true */
  updated_at?: number;
  speed?: number;
  commands?: string[];
  volume?: number;
  muted?: boolean;
}

export type CardCommand = "previous" | "play" | "pause" | "next";
type Reply = { ok?: boolean; error?: string };

const getSendspinCard = callable<[], CardState>("get_sendspin_card");
const getSendspinCardConfig = callable<[], { port: number; nonce: string }>("get_sendspin_card_config");
const sendspinCardCommand = callable<[name: string], Reply>("sendspin_card_command");
const sendspinCardVolume = callable<[value: number], Reply>("sendspin_card_volume");

export const sendCommand = (name: CardCommand) => sendspinCardCommand(name).catch(() => ({ error: "failed" }) as Reply);
export const sendVolume = (value: number) => sendspinCardVolume(value).catch(() => ({ error: "failed" }) as Reply);

const clamp = (value: number, low: number, high: number) => Math.min(high, Math.max(low, value));

/** The position to show now: the reported one moved on by the time since it was true (never past the end, never negative). */
export function shownPosition(card: CardState, nowSeconds: number): number | undefined {
  if (typeof card.position_ms !== "number") return undefined;
  let position = card.position_ms;
  if (card.playing && typeof card.updated_at === "number") {
    position += Math.max(0, nowSeconds - card.updated_at) * 1000 * (card.speed ?? 1);
  }
  const duration = card.duration_ms;
  return clamp(position, 0, typeof duration === "number" && duration > 0 ? duration : Infinity);
}

/** m:ss (h:mm:ss from an hour on) */
export function formatTime(ms: number | undefined): string {
  if (typeof ms !== "number" || !isFinite(ms) || ms < 0) return "";
  const total = Math.floor(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

export function coverUrl(config: { port: number; nonce: string } | undefined, art: string | null | undefined): string | undefined {
  if (!config || !art) return undefined;
  return `http://127.0.0.1:${config.port}/sendspin/cover?nonce=${encodeURIComponent(config.nonce)}&id=${encodeURIComponent(art)}`;
}

/** The card state (loaded once, then following the backend event), the cover route config, and `now` ticking every second while playing. */
export function useCard() {
  const [card, setCard] = useState<CardState>({ active: false });
  const [config, setConfig] = useState<{ port: number; nonce: string } | undefined>();
  const [now, setNow] = useState(() => Date.now() / 1000);
  const latest = useRef(card);

  useEffect(() => {
    let alive = true;
    const apply = (next: CardState) => {
      if (!alive || !next || typeof next !== "object") return;
      latest.current = next;
      setCard(next);
    };
    // The event can arrive before the first read answers: only a read that was not overtaken is applied
    let eventSeen = false;
    const listener = addEventListener<[CardState]>("sendspin_card", (next) => {
      eventSeen = true;
      apply(next);
    });
    getSendspinCard().then((s) => { if (!eventSeen) apply(s); }).catch(() => {});
    getSendspinCardConfig().then((c) => alive && setConfig(c)).catch(() => {});
    return () => {
      alive = false;
      removeEventListener("sendspin_card", listener);
    };
  }, []);

  useEffect(() => {
    if (!card.active || !card.playing) return;
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
    setNow(Date.now() / 1000);
    return () => clearInterval(timer);
  }, [card.active, card.playing, card.updated_at]);

  return { card, config, now };
}
