// hls.js ships the light build under an exports subpath that tsc's resolution
// does not follow; it has the same API as the full build.
declare module "hls.js/light" {
  import Hls from "hls.js";
  export default Hls;
}
