// Loads the card file the way a browser would run it (a plain script), with a `module` object to collect the exports, so the
// pure logic can be tested without a DOM. (The repository's package.json says "type": "module", which would otherwise make
// Node treat the card as an ES module.)
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const FILE = path.join(path.dirname(fileURLToPath(import.meta.url)), "../../custom_components/piston/www/piston-remote-card.js");

export function loadCard() {
  const module = { exports: {} };
  // in this realm (not a new context) so the results compare equal with objects made by the tests
  vm.runInThisContext("(function (module) {" + fs.readFileSync(FILE, "utf8") + "\n})", { filename: FILE })(module);
  return module.exports;
}
