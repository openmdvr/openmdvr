// leaflet-hotline (github.com/iosphere/Leaflet.hotline) does not publish its own
// types -- this is a minimal shim with only the surface HotlinePolyline.tsx
// actually uses. The CommonJS module exports the FACTORY, not the applied plugin
// -- it must be called once with the real Leaflet instance to register
// `L.Hotline`/`L.hotline` (see HotlinePolyline.tsx).
//
// This file must stay a global SCRIPT (NO file-level import/export): adding an
// `export {}` here, to also augment the "leaflet" types in the same file, turns
// THIS "new declaration" into an AUGMENTATION, and an augmentation never
// registers a module that did not exist before -- the "leaflet-hotline" import
// would fail again with TS7016 ("implicitly has an any type"). The real
// "leaflet" augmentation (which DOES need to be a module, with its own `export
// {}`) lives separately in leaflet-augment.d.ts -- never in the same file as
// this declaration.
declare module "leaflet-hotline" {
  import type * as L from "leaflet";

  function attachHotline(leaflet: typeof L): void;
  export default attachHotline;
}
