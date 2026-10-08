// Augments the ALREADY installed "leaflet" module with what leaflet-hotline adds
// -- L.hotline(...)/L.Hotline are not in Leaflet's official types because they
// come from a third-party plugin.
//
// `export {}` is REQUIRED here (unlike leaflet-hotline.d.ts): it makes this file
// a real module, so TypeScript treats `declare module "leaflet"` as an
// AUGMENTATION (adding members to the real @types/leaflet types) instead of a
// new declaration that would REPLACE them entirely -- without it, files that do
// not even import leaflet-hotline (MapView.tsx) break because
// Polyline/DivIcon/etc. stop existing.
export {};

declare module "leaflet" {
  interface HotlineOptions extends PolylineOptions {
    min?: number;
    max?: number;
    palette?: Record<number, string>;
    weight?: number;
    outlineColor?: string;
    outlineWidth?: number;
  }

  // [lat, lon, value] -- the third element is the numeric value that colors that
  // segment (speed, in this project).
  type HotlineLatLng = [number, number, number];

  class Hotline extends Polyline {
    constructor(latlngs: HotlineLatLng[], options?: HotlineOptions);
    setLatLngs(latlngs: HotlineLatLng[]): this;
  }

  function hotline(latlngs: HotlineLatLng[], options?: HotlineOptions): Hotline;
}
