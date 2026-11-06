import { addEventListener, callable, removeEventListener } from "@decky/api";
import { disposeCursor, onActivity, reportScreen, setViewVisible } from "./CursorOverlay";

const viewReady = callable<[], void>("view_ready");

/** Listen for the plugin's cursor events and report the screen size; returns the function that undoes it all. */
export function registerCursorControl(): () => void {
  const activity = addEventListener<[{ down: boolean }]>("cursor_activity", () => onActivity());
  const view = addEventListener<[{ visible: boolean }]>("cursor_view", ({ visible }) => {
    setViewVisible(visible);
    if (!visible) setTimeout(() => viewReady().catch(() => {}), 40); // let the compositor drop the view first
  });
  // Steam's main window may not be ready when the plugin loads: ask again for a while
  let tries = 0;
  const screenTimer = setInterval(() => {
    if (reportScreen() || ++tries > 20) clearInterval(screenTimer);
  }, 1000);
  return () => {
    removeEventListener("cursor_activity", activity);
    removeEventListener("cursor_view", view);
    clearInterval(screenTimer);
    disposeCursor();
  };
}
