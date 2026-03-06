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

// Purple marker for Member institutions
function pinSvg(fill: string, size = 28) {
  const r = Math.max(10, Math.floor(size * 0.36));
  const c = Math.floor(size / 2);
  const svg = encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
      <circle cx="${c}" cy="${c}" r="${r}" fill="${fill}" stroke="white" stroke-width="1.5"/>
    </svg>`
  );
  return `data:image/svg+xml;charset=UTF-8,${svg}`;
}

const MEMBER_ICON = { url: pinSvg("#7c3aed") };       // purple
const SELECTED_ICON = { url: pinSvg("#059669", 34) };  // green, bigger when selected

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

  // Only rows with valid coordinates
  const mappable = useMemo(
    () => rows.filter((r) => r.lat != null && r.lng != null),
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
    mappable.forEach((r) => bounds.extend({ lat: r.lat!, lng: r.lng! }));
    if (mappable.length === 1) {
      map.setCenter({ lat: mappable[0].lat!, lng: mappable[0].lng! });
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

  const noCoords = rows.length - mappable.length;

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
            position={{ lat: r.lat!, lng: r.lng! }}
            icon={selectedId === r.account_id ? SELECTED_ICON : MEMBER_ICON}
            title={r.account_name}
            onClick={() => setSelectedId(r.account_id === selectedId ? null : r.account_id)}
          />
        ))}

        {selectedRow && (
          <InfoWindowF
            position={{ lat: selectedRow.lat!, lng: selectedRow.lng! }}
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

      {/* Badge: how many without coordinates */}
      {noCoords > 0 && (
        <div className="absolute bottom-2 right-2 bg-white/90 rounded px-2 py-1 text-xs text-gray-500 shadow border">
          {mappable.length} of {rows.length} institutions have location data
        </div>
      )}
    </div>
  );
}
