import { Router } from "@decky/ui";
import { useEffect, useState } from "react";

import { PipPosition, setPipState, usePipState } from "./store";
import { UIComposition, useUIComposition } from "./useUIComposition";

// `size` defines a 16:9 bounding box; the stream is fitted inside it so every
// camera takes a similar footprint regardless of its aspect ratio
const BOX_ASPECT = 16 / 9;

interface Bounds { x: number; y: number; width: number; height: number }

// Plugins run in Steam's hidden shared context (window is ~1x1), so the real
// screen size comes from the main Steam UI window; this works on any device/resolution
const screenSize = () => {
  const win: Window | undefined =
    (Router as any).WindowStore?.GamepadUIMainWindowInstance?.BrowserWindow;
  return { width: win?.innerWidth || 1280, height: win?.innerHeight || 720 };
};

const computeBounds = (
  position: PipPosition,
  size: number,
  margin: number,
  aspect: number
): Bounds => {
  const screen = screenSize();
  const boxWidth = screen.width * size;
  const boxHeight = boxWidth / BOX_ASPECT;
  const width = Math.round(Math.min(boxWidth, boxHeight * aspect));
  const height = Math.round(width / aspect);
  const left = margin;
  const centerX = Math.round((screen.width - width) / 2);
  const right = screen.width - width - margin;
  const top = margin;
  const centerY = Math.round((screen.height - height) / 2);
  const bottom = screen.height - height - margin;

  const [x, y] = {
    "top": [centerX, top],
    "top-right": [right, top],
    "right": [right, centerY],
    "bottom-right": [right, bottom],
    "bottom": [centerX, bottom],
    "bottom-left": [left, bottom],
    "left": [left, centerY],
    "top-left": [left, top],
  }[position];
  return { x, y, width, height };
};

const BrowserView = ({ url, bounds }: { url: string; bounds: Bounds }) => {
  useUIComposition(UIComposition.Notification);

  const [view] = useState<any>(() => {
    try {
      const root: any = (Router as any).WindowStore?.GamepadUIMainWindowInstance;
      const created = root.CreateBrowserView("decky-home-assistant-pip");
      setPipState({ error: "" });
      return created;
    } catch (e) {
      console.error("Decky Home Assistant: CreateBrowserView failed", e);
      setPipState({ error: `Browser view unavailable: ${e}`, visible: false });
      return null;
    }
  });

  useEffect(() => {
    if (!view) return;
    return () => view.Destroy();
  }, [view]);

  useEffect(() => {
    view?.LoadURL(url);
  }, [view, url]);

  // The player page reports its state through document.title
  useEffect(() => {
    if (!view) return;
    const token = new URL(url).searchParams.get("t");
    if (!token) return;
    const suffix = ` #${token}`;
    const timer = setInterval(() => {
      const title: string = view.Title ?? "";
      // Unmuted playback needs user activation on the page; until it plays, keep granting it
      // (the page only exists after the load finishes, so one call at load is too early)
      if (!/^playing/.test(title)) {
        try {
          view.GetBrowser().NotifyUserActivation();
        } catch {
          // older Steam builds: the player falls back to muted playback
        }
      }
      if (!title.endsWith(suffix)) return;
      const [, status, aspect] = title.slice(0, -suffix.length).match(/^(.*?)(?: @([\d.]+))?$/) ?? [];
      setPipState({ status, ...(aspect && { aspect: parseFloat(aspect) }) });
    }, 500);
    return () => clearInterval(timer);
  }, [view, url]);

  useEffect(() => {
    if (!view) return;
    const browser = view.GetBrowser();
    browser.SetBounds(bounds.x, bounds.y, bounds.width, bounds.height);
    // The view stays 2x2 and hidden until it is made visible explicitly
    browser.SetVisible(true);
  }, [view, bounds.x, bounds.y, bounds.width, bounds.height]);

  return null;
};

export const PipOverlay = () => {
  const { visible, url, position, size, margin, aspect } = usePipState();
  if (!visible || !url) return null;
  return <BrowserView url={url} bounds={computeBounds(position, size, margin, aspect)} />;
};
