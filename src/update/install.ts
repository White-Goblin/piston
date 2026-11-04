import { addEventListener, callable, removeEventListener, toaster } from "@decky/api";

// Decky Loader's own globals in the Steam UI context (the plugin API has no install call)
declare const DeckyBackend: { call: (route: string, ...args: unknown[]) => Promise<unknown> };
declare const DeckyPluginLoader: any;
declare global {
  interface Window {
    /** Set once per page load of the Steam interface; a plugin reload does not reset it. */
    __pistonUpdateSeen?: boolean;
  }
}

const PLUGIN_NAME = "Piston";
const INSTALL_ROUTE = "utilities/install_plugin";
const INSTALL_TYPE_REINSTALL = 1;
const POLL_MS = 10_000;
const REVERT_WINDOW_MS = 5 * 60_000;       // the new version has this long after it is installed to confirm itself
const GIVE_UP_MS = 2 * 60 * 60_000;        // forget an outstanding request that never resolved
const STORAGE_KEY = "piston.update.outstanding";
const CALL_TIMEOUT_MS = 8_000;             // a call to a plugin backend that has died never settles (found live): give up on it

interface PendingUpdate {
  version: string;
  sha256: string;
  url: string;
  previous: string;
  revert_url: string;
  revert_sha256: string;
}

interface Marker {
  target: string;
  confirmed?: boolean;
}

/** What we asked Decky Loader to install. Kept in localStorage: the plugin's frontend is loaded again by the reinstall. */
interface Outstanding extends PendingUpdate {
  requestedAt: number;
  installedAt?: number;
}

// `requested`: also give an update whose Decky dialog is believed to be open (it is gone when the interface restarted)
const updatePending = callable<[requested?: boolean], PendingUpdate | null>("update_pending");
const updateReport = callable<[result: { result: "requested" | "error"; detail?: string }], void>("update_report");
const updateMarker = callable<[], Marker | null>("update_marker");

/** The promise's result, or a rejection when it has not settled in time (a dead backend neither answers nor fails). */
const withTimeout = <T,>(promise: Promise<T>, ms = CALL_TIMEOUT_MS): Promise<T> =>
  new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("no answer from the plugin backend")), ms);
    promise.then(
      (value) => { clearTimeout(timer); resolve(value); },
      (error) => { clearTimeout(timer); reject(error); }
    );
  });

const log = (...args: unknown[]) => console.log("[Piston][update]", ...args);

const loadOutstanding = (): Outstanding | null => {
  try {
    const text = localStorage.getItem(STORAGE_KEY);
    return text ? (JSON.parse(text) as Outstanding) : null;
  } catch {
    return null;
  }
};

const saveOutstanding = (value: Outstanding | null) => {
  try {
    if (value) localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    // no storage: the revert watch then only lasts until the next reload
  }
};

/** The version of this plugin that Decky Loader lists as installed, or undefined when it cannot be read. */
const installedVersion = (): string | undefined => {
  try {
    const plugins = DeckyPluginLoader.deckyState.publicState().installedPlugins as { name: string; version: string }[];
    return plugins.find((p) => p.name === PLUGIN_NAME)?.version;
  } catch {
    return undefined;
  }
};

/** Ask Decky Loader to install a local zip. Decky shows its own dialog; the user approves it (nothing here confirms it). */
const requestInstall = (url: string, version: string, sha256: string) =>
  DeckyBackend.call(INSTALL_ROUTE, url, PLUGIN_NAME, version, sha256, INSTALL_TYPE_REINSTALL);

/**
 * Hand a waiting update (prepared and verified by the backend) to Decky Loader, and watch that the new version comes up.
 * If it installs but its backend never confirms, ask for the previous version again from the saved copy.
 * Returns the function that stops it.
 */
export const registerUpdateInstaller = () => {
  let stopped = false;
  let busy = false;
  let outstanding = loadOutstanding();
  // First registration since the Steam interface (re)loaded: a dialog opened before is gone, so ask for it again
  const freshPage = !window.__pistonUpdateSeen;
  window.__pistonUpdateSeen = true;

  /** Ask Decky Loader to show its install dialog for this update and tell the backend. */
  const ask = async (pending: PendingUpdate, showToast: boolean) => {
    log("asking Decky Loader to install", pending.version);
    try {
      await requestInstall(pending.url, pending.version, pending.sha256);
    } catch (e) {
      log("the install request failed", e);
      await withTimeout(updateReport({ result: "error", detail: String(e) })).catch(() => {});
      return;
    }
    outstanding = { ...pending, requestedAt: outstanding?.version === pending.version ? outstanding.requestedAt : Date.now(),
                    installedAt: outstanding?.version === pending.version ? outstanding.installedAt : undefined };
    saveOutstanding(outstanding);
    await withTimeout(updateReport({ result: "requested" }));
    if (showToast) approvalToast();
  };

  /** A toast on the device; a tap shows Decky's dialog again (it never approves anything by itself). */
  const approvalToast = () => {
    try {
      toaster.toast({
        title: "Plugin update ready",
        body: "Approve the install in the Decky Loader dialog. Tap here to show the dialog again.",
        duration: 15_000,
        onClick: () => {
          check(true, false);
        },
      });
    } catch (e) {
      log("could not show the toast", e);
    }
  };

  /**
   * `resume`: ask for an update whose dialog is believed to be open as well. True when the Steam interface has just
   * loaded (any dialog from before is gone) and when the toast is tapped.
   */
  const check = async (resume = false, showToast = true) => {
    if (stopped || busy) return;
    busy = true;
    try {
      // The backend only offers an update while it waits for the Steam interface (or, on request, for the dialog), so an
      // earlier request for the same version (a failed, abandoned or lost one) no longer counts: ask again.
      const pending = await withTimeout(updatePending(resume));
      if (pending) await ask(pending, showToast);
    } catch (e) {
      log("could not check for a waiting update", e);
    } finally {
      busy = false;
    }
  };

  const finish = (why: string) => {
    log("done:", why);
    outstanding = null;
    saveOutstanding(null);
  };

  let deadSince: number | undefined; // since when the backend has not answered

  const watch = async () => {
    const wait = outstanding;
    if (stopped || !wait) return;
    const now = Date.now();
    if (now - wait.requestedAt > GIVE_UP_MS) return finish("gave up waiting");
    let marker: Marker | null | undefined;
    try {
      marker = (await withTimeout(updateMarker())) ?? null; // no marker comes back as null or undefined, both mean "none pending"
    } catch {
      marker = undefined; // the backend does not answer
    }
    deadSince = marker === undefined ? (deadSince ?? now) : undefined;
    const installed = installedVersion();
    if (marker && marker.target === wait.version && marker.confirmed) return finish("the new version confirmed itself");
    // The backend answers and no update is pending any more: the new version came up, or the update ended without an install
    if (marker === null) return finish(installed === wait.version ? "the new version runs" : "the update ended");
    if (installed === wait.version && wait.installedAt === undefined) {
      wait.installedAt = now; // the new version is in place: from here it must come up
      saveOutstanding(wait);
    }
    const silent = deadSince !== undefined && now - deadSince > REVERT_WINDOW_MS;
    const unconfirmed = !!marker && !marker.confirmed && wait.installedAt !== undefined && now - wait.installedAt > REVERT_WINDOW_MS;
    if (silent || unconfirmed) {
      log("the new version did not come up: asking for the previous one", wait.previous);
      finish("reverting");
      try {
        await requestInstall(wait.revert_url, wait.previous || "previous", wait.revert_sha256);
      } catch (e) {
        log("the revert request failed", e);
      }
    }
  };

  const listener = addEventListener<[version: string]>("update", () => {
    check();
  });
  const timer = setInterval(watch, POLL_MS);
  check(freshPage);

  return () => {
    stopped = true;
    clearInterval(timer);
    removeEventListener("update", listener);
  };
};
