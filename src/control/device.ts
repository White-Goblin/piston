import { addEventListener, callable, removeEventListener } from "@decky/api";
import { Navigation, Router } from "@decky/ui";

// Steam client globals available in the plugin's SharedJSContext
declare const SteamClient: any;
declare const appStore: any;
declare const SteamUIStore: any;
declare const ControllerStore: any;

const reportAppState = callable<[appid: string, name: string], void>("report_app_state");
const reportAppFocus = callable<[focus: string], void>("report_app_focus");
const reportControllers = callable<[controllers: ControllerBattery[]], void>("report_controllers");
const logWarning = callable<[message: string], void>("log_warning");

// Steam's app_type values for what we list; tools (Proton), DLC, software etc. are left out
const CONTROLLER_POLL_MS = 30_000;
const APP_TYPE_GAME = 1;
const APP_TYPE_SHORTCUT = 1073741824;

interface ControllerBattery {
  serial: string;
  name: string;
  battery: number;
  charging: boolean;
  wireless: boolean;
}

export type GameKind = "steam" | "shortcut";

export interface LibraryGame {
  appid: string;
  name: string;
  kind: GameKind;
}

export interface Library {
  steam: LibraryGame[];
  nonSteam: LibraryGame[];
}

const overviewOf = (appid: string) => {
  try {
    return appStore.GetAppOverviewByAppID(Number(appid));
  } catch {
    return undefined;
  }
};

// local_per_client_data is the per-device state; the plain `installed` flag
// is true for games installed on any machine of the account
const installedHere = (app: any) => app?.local_per_client_data?.installed === true;

export const isInstalled = (appid: string) => installedHere(overviewOf(appid));

const lastPlayed = (app: any) => app.rt_last_time_locally_played || app.rt_last_time_played_or_installed || 0;

/** Games installed on this device: Steam games and non-Steam shortcuts, recently played first. */
export const listLibrary = (): Library => {
  const library: Library = { steam: [], nonSteam: [] };
  try {
    const apps = [...appStore.allApps]
      .filter((a: any) => installedHere(a) && a.display_name)
      .sort((a: any, b: any) => lastPlayed(b) - lastPlayed(a) || a.display_name.localeCompare(b.display_name));
    for (const a of apps) {
      const game = { appid: String(a.appid), name: String(a.display_name) };
      if (a.app_type === APP_TYPE_GAME) library.steam.push({ ...game, kind: "steam" });
      else if (a.app_type === APP_TYPE_SHORTCUT) library.nonSteam.push({ ...game, kind: "shortcut" });
    }
  } catch {
    // Steam's store is not ready; show empty lists
  }
  return library;
};

// Steam games launch by app id, shortcuts by their 64-bit game id
const launchId = (appid: string) => {
  const overview = overviewOf(appid);
  return overview?.app_type === APP_TYPE_SHORTCUT ? String(overview.m_gameid) : appid;
};

// Controllers on Steam's own wireless protocol (Steam Controller and puck). Steam reports a
// flat 100% for Bluetooth pads, so those come from the operating system instead (backend).
const readControllers = (): ControllerBattery[] => {
  try {
    const raw = ControllerStore.GetControllers();
    const list: any[] = Array.isArray(raw) ? raw : [...(raw?.values?.() ?? Object.values(raw ?? {}))];
    return list
      .filter((c) => c?.strSerialNumber && c.bWireless && !c.bBluetooth && c.ucBatteryLevel > 0 && c.ucBatteryLevel <= 100)
      .map((c) => ({
        serial: String(c.strSerialNumber),
        name: String(c.strName || "Controller"),
        battery: Number(c.ucBatteryLevel),
        charging: !!c.bCharging,
        wireless: !!c.bWireless,
      }));
  } catch {
    return [];
  }
};

let runningAppId = "";

const appName = (appid: string) => overviewOf(appid)?.display_name ?? appid;

const onApp = (command: { action: "launch" | "quit"; appid?: string }) => {
  if (command.action === "launch" && command.appid) {
    if (!isInstalled(command.appid)) {
      logWarning(`Not launching ${command.appid}: not installed on this device`).catch(() => {});
      return;
    }
    SteamClient.Apps.RunGame(launchId(command.appid), "", -1, 100);
  } else if (command.action === "quit") {
    const appid = command.appid || runningAppId;
    if (appid) SteamClient.Apps.TerminateApp(launchId(appid), false);
  }
};

// Side menus, as numbered by Steam's MenuStore
const MENU_MAIN = 1;
const MENU_QUICK_ACCESS = 2;

// The remote card's Steam menu and Quick Access tiles act like the physical buttons: open the menu, or close it when it is
// already the one showing.
const onRemoteAction = ({ action }: { action: string }) => {
  try {
    const menu = action === "steam" ? MENU_MAIN : action === "qam" ? MENU_QUICK_ACCESS : 0;
    if (!menu) return;
    const open = (Router as any).WindowStore?.GamepadUIMainWindowInstance?.MenuStore?.GetOpenSideMenu?.();
    if (open === menu) Navigation.CloseSideMenus();
    else if (menu === MENU_MAIN) Navigation.OpenMainMenu();
    else Navigation.OpenQuickAccessMenu();
  } catch (e) {
    console.error("Piston: remote action failed", action, e);
  }
};

const onPower = ({ action }: { action: string }) => {
  if (action === "suspend") SteamClient.System.SuspendPC();
  else if (action === "restart") SteamClient.System.RestartPC();
  else if (action === "shutdown") SteamClient.System.ShutdownPC();
  else if (action === "restart_steam") SteamClient.User.StartRestart(false);
};

/** Handles app/power commands from the backend and reports the running app. */
export function registerDeviceControl(): () => void {
  const appListener = addEventListener<[c: { action: "launch" | "quit"; appid?: string }]>("app", onApp);
  const powerListener = addEventListener<[c: { action: string }]>("power", onPower);
  const remoteActionListener = addEventListener<[c: { action: string }]>("remote_action", onRemoteAction);

  // Read what is running now and report it. Returns false when Steam's store is not ready, so the caller
  // can decide (at load: report nothing running; on a resume request: stay silent, the backend asks again).
  const syncRunningApp = (): boolean => {
    try {
      const running = [...(SteamUIStore.m_runningAppIDs ?? [])];
      runningAppId = running.length ? String(running[0]) : "";
      reportAppState(runningAppId, runningAppId ? appName(runningAppId) : "").catch(() => {});
      return true;
    } catch {
      return false;
    }
  };

  // Apps already running when the plugin loads (e.g. after a reload) get no lifetime event.
  // If Steam's store is not ready, lifetime events still cover new launches
  if (!syncRunningApp()) reportAppState("", "").catch(() => {});

  // After a wake the backend asks for the running app again (a game may have ended during the suspend)
  const syncListener = addEventListener<[]>("app_sync", syncRunningApp);

  // Steam repeats this event every few seconds, so only report changes. Steam's interface
  // is the steamwebhelper window. It is in front when it holds focus (home, library), and
  // also when a menu (Steam button, Quick Access) opens over a game: the game stays the
  // focused app then, but steamwebhelper drops out of the focusable windows.
  let lastFocus = "";
  const focusEvents = SteamClient.System.UI.RegisterForFocusChangeEvents(
    (e: { focusedApp?: { strExeName?: string }; rgFocusable?: { strExeName?: string }[] }) => {
      const isSteam = (w?: { strExeName?: string }) => w?.strExeName === "steamwebhelper";
      const steamFocusable = (e?.rgFocusable ?? []).some(isSteam);
      const focus = isSteam(e?.focusedApp) || !steamFocusable ? "steam" : "app";
      if (focus !== lastFocus) {
        lastFocus = focus;
        reportAppFocus(focus).catch(() => {});
      }
    }
  );

  // Batteries drain slowly, so poll; a controller connecting also triggers a read
  let lastControllers = "";
  const syncControllers = () => {
    const controllers = readControllers();
    const key = JSON.stringify(controllers);
    if (key !== lastControllers) {
      lastControllers = key;
      reportControllers(controllers).catch(() => {});
    }
  };
  syncControllers();
  const controllerTimer = setInterval(syncControllers, CONTROLLER_POLL_MS);
  const controllerChanges = SteamClient.Input.RegisterForActiveControllerChanges(() => syncControllers());

  const lifetime = SteamClient.GameSessions.RegisterForAppLifetimeNotifications(
    (n: { unAppID: number; bRunning: boolean }) => {
      const appid = String(n.unAppID);
      if (n.bRunning) {
        runningAppId = appid;
        reportAppState(appid, appName(appid)).catch(() => {});
      } else if (runningAppId === appid) {
        runningAppId = "";
        reportAppState("", "").catch(() => {});
      }
    }
  );

  return () => {
    removeEventListener("app", appListener);
    removeEventListener("power", powerListener);
    removeEventListener("remote_action", remoteActionListener);
    removeEventListener("app_sync", syncListener);
    lifetime?.unregister?.();
    focusEvents?.unregister?.();
    controllerChanges?.unregister?.();
    clearInterval(controllerTimer);
  };
}
