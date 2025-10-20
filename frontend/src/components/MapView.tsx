// src/components/MapView.tsx
import React, {
  useMemo,
  useState,
  useCallback,
  useEffect,
  useRef,
} from "react";
import {
  GoogleMap,
  MarkerF,
  InfoWindowF,
  useJsApiLoader,
} from "@react-google-maps/api";
import type { ExplorerPoint } from "../lib/api";

/** Contenedor del mapa */
const containerStyle = { height: 420, width: "100%" } as const;

/** Opciones de mapa (tipos “any” para no depender de types globales) */
const mapOptions: any = {
  disableDefaultUI: false,
  streetViewControl: false,
  mapTypeControl: false,
  clickableIcons: false,
};

/** Colores */
const COLOR_PROFILING = "#2563eb";
const COLOR_QUALIF   = "#059669";
const COLOR_BOTH_FILL = "#f59e0b";
const COLOR_BOTH_RING = "#1d4ed8";
const COLOR_NEIGHBOR  = "#9ca3af";
const COLOR_BASE      = "#ef4444";
const COLOR_HIGHLIGHT = "#f59e0b";

/** Iconos SVG inline */
function pinSvg(colorFill: string, colorStroke?: string, size = 28) {
  const stroke = colorStroke
    ? `stroke="${colorStroke}" stroke-width="3"`
    : `stroke="white" stroke-width="1"`;
  const r = Math.max(10, Math.floor(size * 0.36));
  const c = Math.floor(size / 2);
  const svg = encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
      <circle cx="${c}" cy="${c}" r="${r}" fill="${colorFill}" ${stroke}/>
    </svg>`
  );
  return `data:image/svg+xml;charset=UTF-8,${svg}`;
}
const neighborIconUrl = pinSvg(COLOR_NEIGHBOR, undefined, 22);
const baseIconUrl = pinSvg(COLOR_BASE, "#ffffff", 26);

// Halo “highlight” (círculo suave bajo el marker)
function haloSvg(size = 44, fill = COLOR_HIGHLIGHT, fillOpacity = 0.25, stroke = COLOR_HIGHLIGHT, strokeOpacity = 0.9) {
  const r = Math.max(12, Math.floor(size * 0.40));
  const c = Math.floor(size / 2);
  const svg = encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
      <circle cx="${c}" cy="${c}" r="${r}" fill="${fill}" fill-opacity="${fillOpacity}"
              stroke="${stroke}" stroke-opacity="${strokeOpacity}" stroke-width="2"/>
    </svg>`
  );
  return `data:image/svg+xml;charset=UTF-8,${svg}`;
}
const highlightHaloUrl = haloSvg();

function iconFor(point: ExplorerPoint) {
  const both = point.badges?.profiling && point.badges?.qualification;
  if (both) return { url: pinSvg(COLOR_BOTH_FILL, COLOR_BOTH_RING) };
  if (point.badges?.profiling) return { url: pinSvg(COLOR_PROFILING) };
  if (point.badges?.qualification) return { url: pinSvg(COLOR_QUALIF) };
  return { url: pinSvg(COLOR_NEIGHBOR) };
}

// ====== OMS loader con shim de module.exports + memoización ======
let _omsPromise: Promise<any> | null = null;
function loadOMSFromCdn(): Promise<any> {
  if (_omsPromise) return _omsPromise;
  _omsPromise = new Promise((resolve) => {
    const w = window as any;
    if (w.OverlappingMarkerSpiderfier) return resolve(w.OverlappingMarkerSpiderfier);

    const urls = [
      "https://unpkg.com/overlapping-marker-spiderfier@1.0.3/lib/oms.min.js",
      "https://cdn.jsdelivr.net/npm/overlapping-marker-spiderfier@1.0.3/lib/oms.min.js",
    ];

    let i = 0;
    const tryNext = () => {
      if (i >= urls.length) return resolve(null);

      // 1) prelude: crea el identificador global `module`
      const prelude = document.createElement("script");
      prelude.text = `
        (function(){
          try { window.__prevModule__ = window.module; } catch(_) {}
          try { var module = window.module = { exports: {} }; } catch(_) {}
        })();
      `;
      document.head.appendChild(prelude);

      // 2) carga OMS
      const s = document.createElement("script");
      s.src = urls[i++];
      s.async = true;
      s.onload = () => {
        try {
          const g =
            (w as any).OverlappingMarkerSpiderfier ||
            (w as any).module?.exports?.default ||
            (w as any).module?.exports ||
            null;
          if (g && !w.OverlappingMarkerSpiderfier) w.OverlappingMarkerSpiderfier = g;
          resolve(w.OverlappingMarkerSpiderfier || g || null);
        } finally {
          // 3) restaurar `module`
          const restore = document.createElement("script");
          restore.text = `
            (function(){
              try {
                if (window.__prevModule__ === undefined) { try { delete window.module; } catch(_) {} }
                else { window.module = window.__prevModule__; }
                delete window.__prevModule__;
              } catch(_) {}
            })();
          `;
          document.head.appendChild(restore);
          setTimeout(() => { prelude.remove(); restore.remove(); }, 0);
        }
      };
      s.onerror = () => {
        // restaurar y probar el siguiente CDN
        const restore = document.createElement("script");
        restore.text = `
          (function(){
            try {
              if (window.__prevModule__ === undefined) { try { delete window.module; } catch(_) {} }
              else { window.module = window.__prevModule__; }
              delete window.__prevModule__;
            } catch(_) {}
          })();
        `;
        document.head.appendChild(restore);
        setTimeout(() => { prelude.remove(); restore.remove(); }, 0);
        tryNext();
      };
      document.head.appendChild(s);
    };
    tryNext();
  });
  return _omsPromise;
}

// (opcional) pequeño jitter para puntos con coordenadas idénticas
function jitterPointsIfNeeded(arr: ExplorerPoint[], enabled: boolean) {
  if (!enabled) return arr;
  const byKey = new Map<string, ExplorerPoint[]>();
  arr.forEach(p => {
    const k = `${p.lat?.toFixed(6)},${p.lng?.toFixed(6)}`;
    const a = byKey.get(k) || [];
    a.push(p); byKey.set(k, a);
  });
  const out: ExplorerPoint[] = [];
  const R = 0.00008; // ~8–10 m
  byKey.forEach((group) => {
    if (group.length === 1) { out.push(group[0]); return; }
    group.forEach((p, idx) => {
      const angle = (idx / group.length) * 2 * Math.PI;
      out.push({
        ...p,
        lat: (p.lat || 0) + R * Math.cos(angle),
        lng: (p.lng || 0) + R * Math.sin(angle),
      });
    });
  });
  return out;
}

type Props = {
  points: ExplorerPoint[];
  neighborsAll?: ExplorerPoint[];
  selectedAccountId?: string | null;
  onSelectAccount?: (id: string | null) => void;
  onNearbyRequest?: (accountId: string) => void;
  /** Opcional: si lo pasas, aparece botón Details en el popup */
  onShowDetails?: (accountId: string) => void;
  base?: { account_id: string; lat: number; lng: number } | null;
  disableAutoFit?: boolean;
  /** IDs a resaltar (p.ej. enviados desde el Chat) */
  highlightedAccountIds?: string[];
};

export default function MapView({
  points,
  neighborsAll = [],
  selectedAccountId,
  onSelectAccount,
  onNearbyRequest,
  onShowDetails,
  base = null,
  disableAutoFit = false,
  highlightedAccountIds = [],
}: Props) {
  // Estado (controlado / no controlado)
  const [selectedIdUncontrolled, setSelectedIdUncontrolled] = useState<string | null>(null);
  const isControlled = typeof selectedAccountId !== "undefined";
  const selectedId = isControlled ? (selectedAccountId ?? null) : selectedIdUncontrolled;

  const setSelected = useCallback(
    (id: string | null) => {
      onSelectAccount?.(id);
      if (!isControlled) setSelectedIdUncontrolled(id);
    },
    [isControlled, onSelectAccount]
  );

  // API key guard
  const apiKey = (import.meta as any).env?.VITE_GOOGLE_MAPS_API_KEY as string | undefined;
  if (!apiKey || apiKey.trim() === "") {
    return (
      <div className="p-3 border rounded bg-amber-50 text-amber-900">
        Falta <code>VITE_GOOGLE_MAPS_API_KEY</code>. El mapa está desactivado, pero la tabla funciona.
      </div>
    );
  }

  // Conjunto de IDs destacados (O(1) lookup)
  const highlightedSet = useMemo(() => new Set(highlightedAccountIds || []), [highlightedAccountIds]);

  const { isLoaded, loadError } = useJsApiLoader({
    id: "cts-maps-loader",
    googleMapsApiKey: apiKey,
  });

  // Centro: seleccionado > primer point > neighbor > París
  const center = useMemo(() => {
    const byId = new Map<string, ExplorerPoint>();
    points.forEach((p) => byId.set(p.account_id, p));
    neighborsAll.forEach((p) => { if (!byId.has(p.account_id)) byId.set(p.account_id, p); });

    if (selectedId && byId.get(selectedId)?.lat && byId.get(selectedId)?.lng) {
      const p = byId.get(selectedId)!;
      return { lat: p.lat!, lng: p.lng! };
    }
    const p =
      points.find((p) => p.lat && p.lng) ||
      neighborsAll.find((p) => p.lat && p.lng);
    return p ? { lat: p.lat!, lng: p.lng! } : { lat: 48.8566, lng: 2.3522 };
  }, [points, neighborsAll, selectedId]);

  const onMarkerClick = useCallback((id: string) => setSelected(id), [setSelected]);
  const onMapClick = useCallback(() => setSelected(null), [setSelected]);

  // Evita duplicados: quita de neighbors los que ya están en points
  const pointsIds = useMemo(() => new Set(points.map((p) => p.account_id)), [points]);
  const neighborsOnly = useMemo(
    () => (neighborsAll || []).filter((n) => !pointsIds.has(n.account_id)),
    [neighborsAll, pointsIds]
  );

  // Mapa + Spiderfier
  const [map, setMap] = useState<any | null>(null);
  const spiderfierRef = useRef<any | null>(null);
  const [omsReady, setOmsReady] = useState(false); // ← para saber si OMS está operativo

  // Si OMS no está operativo, desplazamos mínimamente puntos coincidentes
  const renderPoints = useMemo(
    () => (omsReady ? points : jitterPointsIfNeeded(points, true)),
    [points, omsReady]
  );

  // Crea / limpia OMS cuando el mapa está listo
  useEffect(() => {
    let disposed = false;

    async function setupOms(m: any) {
      try {
        // 1) Asegura que la librería UMD está disponible en window
        const OverlappingMarkerSpiderfier = await loadOMSFromCdn();
        if (!OverlappingMarkerSpiderfier) {
          setOmsReady(false);      // 👉 usará onClick de MarkerF
          return;
        }
        if (disposed) return;

        // 2) Instancia OMS
        const oms = new OverlappingMarkerSpiderfier(m, {
          keepSpiderfied: true,
          markersWontMove: true,
          markersWontHide: true,
          nearbyDistance: 1,
          circleSpiralSwitchover: 2,
          spiralFootSeparation: 26,
          spiralLengthStart: 32,
          spiralLengthFactor: 5,
          circleFootSeparation: 28,
        });

        oms.addListener("click", (gmMarker: any) => {
          const id = gmMarker.get("account_id") as string | undefined;
          if (id) setSelected(id);
        });

        spiderfierRef.current = oms;
        setOmsReady(true);
      } catch (e) {
        console.error("OMS load/setup failed:", e);
        setOmsReady(false); // caeremos al fallback onClick de MarkerF
      }
    }

    if (map && (window as any).google) {
      setupOms(map);
    }

    return () => {
      disposed = true;
      try { spiderfierRef.current?.clearMarkers(); } catch {}
      spiderfierRef.current = null;
      setOmsReady(false);
    };
  }, [map, setSelected]);

  // Helpers para registrar / desregistrar markers con OMS
  const makeRegisterMarker = (id: string) => (m: any | null) => {
    if (!m || !spiderfierRef.current || !omsReady) return;
    try {
      m.set("account_id", id);
      spiderfierRef.current.addMarker(m);
      // DEBUG opcional:
      // console.debug("OMS add", id, m.getPosition()?.toUrlValue?.(6));
    } catch {}
  };

  const makeUnregisterMarker =
    (_id: string) => (m: any | null) => {
      if (!m || !spiderfierRef.current) return;
      try { spiderfierRef.current.removeMarker(m); } catch {}
    };

  // Auto-fit
  useEffect(() => {
    if (!map || disableAutoFit) return;
    const all = [
      ...(points || []),
      ...(neighborsOnly || []),
      ...(base ? [{ account_id: base.account_id, lat: base.lat, lng: base.lng } as any] : []),
    ].filter((p) => p.lat && p.lng);
    if (!all.length) return;
    const bounds = new (window as any).google.maps.LatLngBounds();
    all.forEach((p) => bounds.extend({ lat: p.lat!, lng: p.lng! }));
    map.fitBounds(bounds, 48);
  }, [map, points, neighborsOnly, base, disableAutoFit]);

  if (loadError) {
    return (
      <div className="p-3 border rounded bg-rose-50 text-rose-900">
        Error cargando Google Maps. Revisa la API key. La tabla sigue operativa.
      </div>
    );
  }
  if (!isLoaded) return <div className="p-3">Cargando mapa…</div>;

  return (
    <GoogleMap
      mapContainerStyle={containerStyle}
      center={center}
      zoom={5}
      options={mapOptions}
      onClick={onMapClick}
      onLoad={setMap}
    >
      {/* Base (opcional) */}
      {base?.lat != null && base?.lng != null && (
        <MarkerF
          position={{ lat: base.lat, lng: base.lng }}
          icon={{ url: baseIconUrl }}
          zIndex={50}
        />
      )}

      {/* Vecinos SIN filtros (gris) */}
      {neighborsOnly
        .filter((p) => p.lat && p.lng)
        .map((p) => (
          <MarkerF
            key={`nbr-${p.account_id}`}
            position={{ lat: p.lat!, lng: p.lng! }}
            icon={{ url: neighborIconUrl }}
            zIndex={1}
            // cuando OMS está listo dejamos el click a OMS; si no, fallback
            onClick={omsReady ? undefined : () => onMarkerClick(p.account_id)}
            onLoad={makeRegisterMarker(p.account_id)}
            onUnmount={makeUnregisterMarker(p.account_id)}
          />
        ))}

      {/* Puntos FILTRADOS */}
      {renderPoints
        .filter((p) => p.lat && p.lng)
        .map((p) => {
          const isSelected = selectedId === p.account_id;
          const isHighlighted = highlightedSet.has(p.account_id); // <-- FIX
          return (
            <MarkerF
              key={p.account_id}
              position={{ lat: p.lat!, lng: p.lng! }}
              icon={iconFor(p)}
              zIndex={isSelected ? 100 : (isHighlighted ? 20 : 10)}
              // cuando OMS está listo dejamos el click a OMS; si no, fallback
              onClick={omsReady ? undefined : () => onMarkerClick(p.account_id)}
              onLoad={makeRegisterMarker(p.account_id)}
              onUnmount={makeUnregisterMarker(p.account_id)}
            >
              {isSelected && (
                <InfoWindowF
                  position={{ lat: p.lat!, lng: p.lng! }}
                  onCloseClick={() => setSelected(null)}
                >
                  <div className="text-sm">
                    <div className="font-semibold">{p.account_name}</div>
                    <div>{p.country ?? "-"} / {p.city ?? "-"}</div>
                    <div className="mt-1">
                      <span className={p.badges?.profiling ? "text-emerald-700" : "text-gray-400"}>Profiling</span>{" "}·{" "}
                      <span className={p.badges?.qualification ? "text-blue-700" : "text-gray-400"}>Qualification</span>
                    </div>

                    <div className="mt-2 flex gap-2">
                      {onNearbyRequest && (
                        <button
                          className="rounded-md border px-2 py-1 text-xs hover:bg-gray-50"
                          onClick={() => onNearbyRequest(p.account_id)}
                          title="Find nearby (driving distance)"
                        >
                          Nearby…
                        </button>
                      )}
                      {onShowDetails && (
                        <button
                          className="rounded-md border px-2 py-1 text-xs hover:bg-gray-50"
                          onClick={() => onShowDetails(p.account_id)}
                          title="Show details (Member / PI)"
                        >
                          Details…
                        </button>
                      )}
                    </div>
                  </div>
                </InfoWindowF>
              )}
            </MarkerF>
          );
        })}

      {/* Halos de highlight: se pintan DESPUÉS para estar “encima” del suelo pero debajo del pin (zIndex menor que el pin) */}
      {renderPoints
        .filter((p) => p.lat && p.lng && highlightedSet.has(p.account_id))
        .map((p) => (
          <MarkerF
            key={`hl-${p.account_id}`}
            position={{ lat: p.lat!, lng: p.lng! }}
            icon={{ url: highlightHaloUrl }}
            zIndex={15}
            // El halo no debe interceptar los clics si OMS está activo
            onClick={omsReady ? undefined : () => onMarkerClick(p.account_id)}
            // No registramos en OMS para no interferir con el clustering de clics
          />
        ))}
    </GoogleMap>
  );
}