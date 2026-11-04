import { addEventListener, callable, removeEventListener } from "@decky/api";

// Decky Loader's own globals in the Steam UI context (the plugin API has no install call)
declare const DeckyBackend: { call: (route: string, ...args: unknown[]) => Promise<unknown> };
declare const DeckyPluginLoader: any;

const PLUGIN_NAME = "Decky Home Assistant";
const INSTALL_ROUTE = "utilities/install_plugin";
const INSTALL_TYPE_REINSTALL = 1;
const POLL_MS = 10_000;
const REVERT_WINDOW_MS = 5 * 60_000;       // the new version has this long after it is installed to confirm itself
const GIVE_UP_MS = 2 * 60 * 60_000;        // forget an outstanding request that never resolved
const STORAGE_KEY = "deckyha.update.outstanding";

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

const updatePending = callable<[], PendingUpdate | null>("update_pending");
const updateReport = callable<[result: { result: "requested" | "error"; detail?: string }], void>("update_report");
const updateMarker = callable<[], Marker | null>("update_marker");

const log = (...args: unknown[]) => console.log("[DeckyHA][update]", ...args);

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

  const check = async () => {
    if (stopped || busy) return;
    busy = true;
    try {
      const pending = await updatePending();
      // The backend only offers an update while it waits for the Steam interface, so an earlier request for the same
      // version (a failed or abandoned one) no longer counts: ask again.
      if (!pending) return;
      log("asking Decky Loader to install", pending.version);
      try {
        await requestInstall(pending.url, pending.version, pending.sha256);
      } catch (e) {
        log("the install request failed", e);
        await updateReport({ result: "error", detail: String(e) });
        return;
      }
      outstanding = { ...pending, requestedAt: Date.now() };
      saveOutstanding(outstanding);
      await updateReport({ result: "requested" });
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
      marker = (await updateMarker()) ?? null; // no marker comes back as null or undefined, both mean "none pending"
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
  check();

  return () => {
    stopped = true;
    clearInterval(timer);
    removeEventListener("update", listener);
  };
};
