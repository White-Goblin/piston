import { useSyncExternalStore } from "react";

export type PipPosition =
  | "top" | "top-right" | "right" | "bottom-right"
  | "bottom" | "bottom-left" | "left" | "top-left";

export interface PipState {
  visible: boolean;
  url: string;
  position: PipPosition;
  /** Width as a fraction of the screen width */
  size: number;
  /** Margin in px from the screen edge */
  margin: number;
  /** Set when the Steam browser view API is unavailable */
  error: string;
  /** Player state reported through the page title (hidden, connecting, playing, error: ...) */
  status: string;
  /** Width / height of the stream, once the player has reported it */
  aspect: number;
}

let state: PipState = {
  visible: false,
  url: "",
  position: "top-right",
  size: 0.4,
  margin: 30,
  error: "",
  status: "hidden",
  aspect: 16 / 9,
};
const listeners = new Set<() => void>();

export const getPipState = () => state;

export const setPipState = (patch: Partial<PipState>) => {
  state = { ...state, ...patch };
  listeners.forEach((l) => l());
};

export const subscribePipState = (cb: (s: PipState) => void) => {
  const listener = () => cb(state);
  listeners.add(listener);
  return () => listeners.delete(listener);
};

export const usePipState = () =>
  useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    getPipState
  );

export const showPip = (url: string, patch: Partial<PipState> = {}) =>
  setPipState({ ...patch, url, visible: true, status: "loading", aspect: 16 / 9 });

export const hidePip = () => setPipState({ visible: false, status: "hidden" });
