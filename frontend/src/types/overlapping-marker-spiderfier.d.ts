declare module "overlapping-marker-spiderfier" {
  export interface OMSOptions {
    keepSpiderfied?: boolean;
    markersWontMove?: boolean;
    markersWontHide?: boolean;
    nearbyDistance?: number;
    circleSpiralSwitchover?: number;
    spiralFootSeparation?: number;
    spiralLengthStart?: number;
    spiralLengthFactor?: number;
    circleFootSeparation?: number;
  }

  export default class OverlappingMarkerSpiderfier {
    constructor(map: google.maps.Map, options?: OMSOptions);

    addMarker(marker: google.maps.Marker): void;
    removeMarker(marker: google.maps.Marker): void;
    clearMarkers(): void;

    // Eventos comunes
    addListener(event: "click" | "spiderfy" | "unspiderfy", fn: (marker: google.maps.Marker) => void): void;
  }
}