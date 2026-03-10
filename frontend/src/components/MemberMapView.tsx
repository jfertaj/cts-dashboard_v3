// src/components/MemberMapView.tsx
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  GoogleMap,
  MarkerF,
  InfoWindowF,
  useJsApiLoader,
} from "@react-google-maps/api";
import { displayCountry } from "../lib/countryUtils";

type MemberRow = {
  account_id: string;
  account_name: string;
  country: string;
  city: string;
  lat?: number | null;
  lng?: number | null;
  data: Record<string, any>;
};

const containerStyle = { height: 420, width: "100%" } as const;

const mapOptions: any = {
  disableDefaultUI: false,
  streetViewControl: false,
  mapTypeControl: false,
  clickableIcons: false,
};

// INNODIA crystal marker icon — normal size
const CRYSTAL_SIZE   = 34;
const CRYSTAL_SIZE_SEL = 46;

// Country centroid fallback — used when ShippingLatitude is null in SF
const COUNTRY_CENTROIDS: Record<string, { lat: number; lng: number }> = {
  AT: { lat: 47.516, lng: 14.550 }, BE: { lat: 50.503, lng: 4.470 },
  BG: { lat: 42.734, lng: 25.486 }, CH: { lat: 46.818, lng: 8.228 },
  CY: { lat: 35.126, lng: 33.430 }, CZ: { lat: 49.818, lng: 15.473 },
  DE: { lat: 51.166, lng: 10.452 }, DK: { lat: 56.264, lng: 9.502 },
  EE: { lat: 58.595, lng: 25.014 }, ES: { lat: 40.464, lng: -3.749 },
  FI: { lat: 61.924, lng: 25.748 }, FR: { lat: 46.228, lng: 2.214 },
  GB: { lat: 55.378, lng: -3.436 }, GR: { lat: 39.074, lng: 21.824 },
  HR: { lat: 45.100, lng: 15.200 }, HU: { lat: 47.163, lng: 19.503 },
  IE: { lat: 53.142, lng: -7.692 }, IL: { lat: 31.046, lng: 34.852 },
  IT: { lat: 41.872, lng: 12.567 }, LT: { lat: 55.169, lng: 23.881 },
  LU: { lat: 49.815, lng: 6.130 },  LV: { lat: 56.880, lng: 24.603 },
  MT: { lat: 35.938, lng: 14.375 }, NL: { lat: 52.133, lng: 5.291 },
  NO: { lat: 60.472, lng: 8.469 },  PL: { lat: 51.919, lng: 19.145 },
  PT: { lat: 39.400, lng: -8.225 }, RO: { lat: 45.943, lng: 24.967 },
  RS: { lat: 44.017, lng: 21.006 }, SE: { lat: 60.128, lng: 18.644 },
  SI: { lat: 46.151, lng: 14.996 }, SK: { lat: 48.669, lng: 19.699 },
  TR: { lat: 38.964, lng: 35.243 }, US: { lat: 37.090, lng: -95.713 },
  CA: { lat: 56.130, lng: -106.347 }, AU: { lat: -25.274, lng: 133.775 },
};

// Deterministic jitter so same-country members don't stack on one pixel
function _jitter(id: string): { dlat: number; dlng: number } {
  let h = 0;
  for (let i = 0; i < id.length; i++) { h = Math.imul(31, h) + id.charCodeAt(i) | 0; }
  const u = ((h >>> 0) & 0xFFFF) / 0xFFFF;
  const v = ((h >>> 16) & 0xFFFF) / 0xFFFF;
  return { dlat: (u - 0.5) * 2.5, dlng: (v - 0.5) * 3.5 };
}

function resolveCoords(r: MemberRow): { lat: number; lng: number } | null {
  if (r.lat != null && r.lng != null) return { lat: r.lat, lng: r.lng };
  const c = COUNTRY_CENTROIDS[r.country];
  if (!c) return null;
  const j = _jitter(r.account_id);
  return { lat: c.lat + j.dlat, lng: c.lng + j.dlng };
}

export default function MemberMapView({
  rows,
  onOpenDetail,
}: {
  rows: MemberRow[];
  onOpenDetail?: (id: string, name: string) => void;
}) {
  const apiKey = (import.meta as any).env?.VITE_GOOGLE_MAPS_API_KEY as string | undefined;

  const { isLoaded, loadError } = useJsApiLoader({
    id: "cts-maps-loader",       // reuse same loader as MapView — no double load
    googleMapsApiKey: apiKey || "",
  });

  const [map, setMap] = useState<any>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Build Google Maps icon objects — requires Maps API to be loaded
  const memberIcon = useMemo(() => {
    if (!isLoaded) return undefined;
    const g = (window as any).google.maps;
    return {
      url: "/innodia_cristal.png",
      scaledSize: new g.Size(CRYSTAL_SIZE, CRYSTAL_SIZE),
      anchor: new g.Point(CRYSTAL_SIZE / 2, CRYSTAL_SIZE / 2),
    };
  }, [isLoaded]);

  const selectedIcon = useMemo(() => {
    if (!isLoaded) return undefined;
    const g = (window as any).google.maps;
    return {
      url: "/innodia_cristal.png",
      scaledSize: new g.Size(CRYSTAL_SIZE_SEL, CRYSTAL_SIZE_SEL),
      anchor: new g.Point(CRYSTAL_SIZE_SEL / 2, CRYSTAL_SIZE_SEL / 2),
    };
  }, [isLoaded]);

  // Resolve coordinates for each row (SF lat/lng or country centroid + jitter)
  const mappable = useMemo(
    () => rows.flatMap((r) => {
      const pos = resolveCoords(r);
      return pos ? [{ ...r, _lat: pos.lat, _lng: pos.lng }] : [];
    }),
    [rows]
  );

  const selectedRow = useMemo(
    () => mappable.find((r) => r.account_id === selectedId) ?? null,
    [mappable, selectedId]
  );

  // Fit bounds whenever rows change
  const prevBoundsKeyRef = useRef<string>("");
  useEffect(() => {
    if (!map || mappable.length === 0) return;
    const key = mappable.map((r) => r.account_id).join(",");
    if (key === prevBoundsKeyRef.current) return;
    prevBoundsKeyRef.current = key;

    const bounds = new (window as any).google.maps.LatLngBounds();
    mappable.forEach((r) => bounds.extend({ lat: r._lat, lng: r._lng }));
    if (mappable.length === 1) {
      map.setCenter({ lat: mappable[0]._lat, lng: mappable[0]._lng });
      map.setZoom(7);
    } else {
      map.fitBounds(bounds, 40);
    }
  }, [map, mappable]);

  const onLoad = useCallback((m: any) => setMap(m), []);
  const onUnmount = useCallback(() => setMap(null), []);

  if (!apiKey || apiKey.trim() === "") {
    return (
      <div className="p-3 border rounded bg-amber-50 text-amber-900 text-sm">
        Missing <code>VITE_GOOGLE_MAPS_API_KEY</code>. Map is disabled.
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="p-3 border rounded bg-red-50 text-red-800 text-sm">
        Failed to load Google Maps.
      </div>
    );
  }

  if (!isLoaded) {
    return (
      <div className="h-[420px] flex items-center justify-center bg-gray-100 rounded-xl border text-sm text-gray-500">
        Loading map…
      </div>
    );
  }

  return (
    <div className="relative rounded-xl border overflow-hidden shadow-sm">
      <GoogleMap
        mapContainerStyle={containerStyle}
        defaultCenter={{ lat: 50, lng: 10 }}
        defaultZoom={4}
        options={mapOptions}
        onClick={() => setSelectedId(null)}
        onLoad={onLoad}
        onUnmount={onUnmount}
      >
        {mappable.map((r) => (
          <MarkerF
            key={r.account_id}
            position={{ lat: r._lat, lng: r._lng }}
            icon={selectedId === r.account_id ? selectedIcon : memberIcon}
            title={r.account_name}
            onClick={() => setSelectedId(r.account_id === selectedId ? null : r.account_id)}
          />
        ))}

        {selectedRow && (
          <InfoWindowF
            position={{ lat: selectedRow._lat, lng: selectedRow._lng }}
            onCloseClick={() => setSelectedId(null)}
          >
            <div className="text-sm max-w-[220px]">
              <div className="font-semibold text-gray-900 leading-tight mb-1">
                {selectedRow.account_name}
              </div>
              <div className="text-gray-600 text-xs mb-1">
                {displayCountry(selectedRow.country)}{selectedRow.city ? ` · ${selectedRow.city}` : ""}
              </div>
              {selectedRow.data["sf.C_Level_of_Membership__c"] && (
                <div className="text-gray-500 text-xs mb-1.5">
                  {selectedRow.data["sf.C_Level_of_Membership__c"]}
                </div>
              )}
              {/* Role pills */}
              <div className="flex flex-wrap gap-1 mb-2">
                {selectedRow.data["sf.Clinical_Trial_Site_CTS_validated__c"] && (
                  <span className="text-xs bg-green-100 text-green-800 border border-green-200 rounded px-1.5 py-0.5">Val. CTS</span>
                )}
                {selectedRow.data["sf.Clinical_Site_CS_validated__c"] && (
                  <span className="text-xs bg-green-100 text-green-800 border border-green-200 rounded px-1.5 py-0.5">Val. CS</span>
                )}
                {selectedRow.data["sf.Diagnostic_Lab_DxLab_validated__c"] && (
                  <span className="text-xs bg-green-100 text-green-800 border border-green-200 rounded px-1.5 py-0.5">Val. DxLab</span>
                )}
                {selectedRow.data["sf.Research_Mechanistic_Lab_LAB_validated__c"] && (
                  <span className="text-xs bg-green-100 text-green-800 border border-green-200 rounded px-1.5 py-0.5">Val. LAB</span>
                )}
              </div>
              <div className="flex gap-2 text-xs text-gray-500">
                <span>{selectedRow.data["extra.SubAccountsCount"] ?? 0} sites</span>
                <span>{selectedRow.data["extra.ContactsCount"] ?? 0} contacts</span>
              </div>
              {onOpenDetail && (
                <button
                  className="mt-2 w-full text-center text-xs text-blue-600 hover:underline"
                  onClick={() => {
                    setSelectedId(null);
                    onOpenDetail(selectedRow.account_id, selectedRow.account_name);
                  }}
                >
                  View details →
                </button>
              )}
            </div>
          </InfoWindowF>
        )}
      </GoogleMap>

      {/* Badge: approximate location note when using country centroids */}
      {mappable.some((r) => r.lat == null) && (
        <div className="absolute bottom-2 right-2 bg-white/90 rounded px-2 py-1 text-xs text-gray-500 shadow border">
          Locations approximated by country
        </div>
      )}
    </div>
  );
}
