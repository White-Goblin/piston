import { addEventListener, callable, removeEventListener, toaster } from "@decky/api";
import { ReactNode } from "react";
import {
  FaBell, FaBolt, FaCamera, FaCar, FaDoorOpen, FaExclamationTriangle, FaFire,
  FaHome, FaLightbulb, FaLock, FaTemperatureHigh, FaTint, FaWalking
} from "react-icons/fa";

// Icon names accepted in the `icon` field of the show_toast action
const ICONS: Record<string, ReactNode> = {
  bell: <FaBell />,
  bolt: <FaBolt />,
  camera: <FaCamera />,
  car: <FaCar />,
  door: <FaDoorOpen />,
  fire: <FaFire />,
  home: <FaHome />,
  light: <FaLightbulb />,
  lock: <FaLock />,
  motion: <FaWalking />,
  temperature: <FaTemperatureHigh />,
  warning: <FaExclamationTriangle />,
  water: <FaTint />,
};

interface Notification {
  title: string;
  message: string;
  duration: number;
  icon?: string;
  id?: string;
  silent?: boolean;
}

const reportToastClick = callable<[id: string], void>("report_toast_click");

/** Shows toasts sent by Home Assistant and tells the backend when one is clicked. Returns the cleanup function. */
export function registerToasts(): () => void {
  const listener = addEventListener<[n: Notification]>("notification", (n) => {
    toaster.toast({
      title: n.title,
      body: n.message,
      icon: n.icon ? ICONS[n.icon] : undefined,
      duration: n.duration,
      // eType 2 is the only value found that suppresses the toast sound
      eType: n.silent ? 2 : undefined,
      onClick: () => {
        reportToastClick(n.id ?? "").catch(() => {});
      },
    });
  });
  return () => removeEventListener("notification", listener);
}
