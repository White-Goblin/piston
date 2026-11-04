import { useSyncExternalStore } from "react";

let registered = false;
const listeners = new Set<() => void>();
export const setRegistered = (value: boolean) => {
  registered = value;
  listeners.forEach((l) => l());
};

/** True while the card lives in the Quick Access tab (the panel then does not show it: one placement at a time). */
export function useTabRegistered(): boolean {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => registered
  );
}
