// Adapted from rossimo/decky-pip (BSD-3-Clause)
import { findModuleChild } from "@decky/ui";

export enum UIComposition {
  Hidden = 0,
  Notification = 1,
  Overlay = 2,
  Opaque = 3,
  OverlayKeyboard = 4,
}

type UseUIComposition = (composition: UIComposition) => {
  releaseComposition: () => void;
};

// Steam's internal hook that keeps the UI composited above the running game
export const useUIComposition: UseUIComposition = findModuleChild((m) => {
  if (typeof m !== "object") return undefined;
  for (const prop in m) {
    if (
      typeof m[prop] === "function" &&
      m[prop].toString().includes("AddMinimumCompositionStateRequest") &&
      m[prop].toString().includes("ChangeMinimumCompositionStateRequest") &&
      m[prop].toString().includes("RemoveMinimumCompositionStateRequest") &&
      !m[prop].toString().includes("m_mapCompositionStateRequests")
    ) {
      return m[prop];
    }
  }
});
