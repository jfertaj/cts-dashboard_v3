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
  draggableCursor: "default",
};

/** Colores */
const COLOR_REFERRAL = "#f97316";      // orange for referral/outreach
const COLOR_INNODIA  = "#0ea5e9";      // blue for INNODIA clinical site
const COLOR_DETECT   = "#a855f7";      // purple for DETECT only
const COLOR_NEUTRAL  = "#9ca3af";      // grey for default
const COLOR_NEIGHBOR  = "#cbd5f5";
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
  const cs = point.cs || {};
  // keep backwards compatibility: derive from point.cs (bootstrap)
  if (cs.clinical && cs.detect) {
    // split INNODIA (left) / DETECT (right)
    const svg = splitPinSvg(COLOR_INNODIA, COLOR_DETECT);
    return { url: svg };
  }
  if (cs.referral) return { url: pinSvg(COLOR_REFERRAL) };
  if (cs.clinical) return { url: pinSvg(COLOR_INNODIA) };
  if (cs.detect) return { url: pinSvg(COLOR_DETECT) };
  return { url: pinSvg(COLOR_NEUTRAL) };
}

function splitPinSvg(leftColor: string, rightColor: string, size = 28) {
  const c = Math.floor(size / 2);
  const svg = encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
       <defs>
         <clipPath id="leftClip"><rect x="0" y="0" width="${c}" height="${size}"/></clipPath>
         <clipPath id="rightClip"><rect x="${c}" y="0" width="${c}" height="${size}"/></clipPath>
       </defs>
       <circle cx="${c}" cy="${c}" r="${Math.max(10, Math.floor(size * 0.36))}" fill="${leftColor}" clip-path="url(#leftClip)" />
       <circle cx="${c}" cy="${c}" r="${Math.max(10, Math.floor(size * 0.36))}" fill="${rightColor}" clip-path="url(#rightClip)" />
       <circle cx="${c}" cy="${c}" r="${Math.max(10, Math.floor(size * 0.36))}" fill="none" stroke="white" stroke-width="1" />
    </svg>`
  );
  return `data:image/svg+xml;charset=UTF-8,${svg}`;
}

function iconForCs(cs: { clinical?: boolean | null; referral?: boolean | null; detect?: boolean | null } | undefined) {
  const c = cs || {} as { clinical?: boolean | null; referral?: boolean | null; detect?: boolean | null };
  if (c.clinical && c.detect) {
    const svg = splitPinSvg(COLOR_INNODIA, COLOR_DETECT);
    return { url: svg };
  }
  if (c.referral) return { url: pinSvg(COLOR_REFERRAL) };
  if (c.clinical) return { url: pinSvg(COLOR_INNODIA) };
  if (c.detect) return { url: pinSvg(COLOR_DETECT) };
  return { url: pinSvg(COLOR_NEUTRAL) };
}

// ====== OMS loader con shim de module.exports + memoización ======
let _omsPromise: Promise<any> | null = null;
const OMS_VERSION = "1.1.4";
function loadOMSFromCdn(): Promise<any> {
  if (_omsPromise) return _omsPromise;
  _omsPromise = new Promise((resolve) => {
    const w = window as any;
    if (w.OverlappingMarkerSpiderfier) return resolve(w.OverlappingMarkerSpiderfier);

    const trySequential = async () => {
      // 1) Prefer loading the raw source bundled in the app (no external network).
      try {
        const mod: any = await import("overlapping-marker-spiderfier/lib/oms.js?raw");
        const code = typeof mod === "string" ? mod : mod?.default;
        if (code) {
          const prevModule = w.module;
          const shim = { exports: {} as any };
          try {
            w.module = shim;
            const factory = new Function(
              "window",
              "module",
              "exports",
              `${code}\nreturn window.OverlappingMarkerSpiderfier || module.exports?.default || module.exports || null;`
            );
            const g = factory.call(w, w, shim, shim.exports);
            const resolved =
              g ||
              w.OverlappingMarkerSpiderfier ||
              shim.exports?.default ||
              shim.exports ||
              null;
            if (resolved) {
              if (!w.OverlappingMarkerSpiderfier) w.OverlappingMarkerSpiderfier = resolved;
              return resolved;
            }
          } finally {
            if (prevModule === undefined) {
              try { delete w.module; } catch {}
            } else {
              w.module = prevModule;
            }
          }
        }
      } catch (err) {
        console.warn?.("MapView: OMS inline bundle load failed; falling back to CDN", err);
      }

      // 2) Fallback to dynamic import from the bundled dependency (might not exist in legacy builds).
      try {
        const mod: any = await import("overlapping-marker-spiderfier");
        const resolved =
          mod?.default ||
          mod?.OverlappingMarkerSpiderfier ||
          mod ||
          w.OverlappingMarkerSpiderfier ||
          null;
        if (resolved) {
          if (!w.OverlappingMarkerSpiderfier) w.OverlappingMarkerSpiderfier = resolved;
          return resolved;
        }
      } catch (err) {
        console.warn?.("MapView: OMS package import failed; falling back to CDN", err);
      }

      // 3) Fallback to CDN fetch when the dependency is missing in the bundle
      //    (e.g., legacy builds). Use multiple common layouts.
      const urls = [
        `https://unpkg.com/overlapping-marker-spiderfier@${OMS_VERSION}/lib/oms.min.js`,
        `https://cdn.jsdelivr.net/npm/overlapping-marker-spiderfier@${OMS_VERSION}/lib/oms.min.js`,
        `https://unpkg.com/overlapping-marker-spiderfier@${OMS_VERSION}/dist/oms.min.js`,
        `https://cdn.jsdelivr.net/npm/overlapping-marker-spiderfier@${OMS_VERSION}/dist/oms.min.js`,
        `https://unpkg.com/overlapping-marker-spiderfier@${OMS_VERSION}/oms.min.js`,
        `https://cdn.jsdelivr.net/npm/overlapping-marker-spiderfier@${OMS_VERSION}/oms.min.js`,
      ];

      for (const url of urls) {
        try {
          const res = await fetch(url, { mode: "cors" });
          if (!res.ok) {
            console.warn?.("MapView: OMS fetch failed (status)", url, res.status);
            continue;
          }
          const code = await res.text();
          const prevModule = w.module;
          const shim = { exports: {} as any };
          try {
            w.module = shim;
            const factory = new Function(
              "window",
              "module",
              "exports",
              `${code}\nreturn window.OverlappingMarkerSpiderfier || module.exports?.default || module.exports || null;`
            );
            const g = factory.call(w, w, shim, shim.exports);
            const resolved =
              g ||
              w.OverlappingMarkerSpiderfier ||
              shim.exports?.default ||
              shim.exports ||
              null;
            if (resolved) {
              if (!w.OverlappingMarkerSpiderfier) w.OverlappingMarkerSpiderfier = resolved;
              return resolved;
            }
          } catch (err) {
            console.error?.("MapView: OMS evaluation failed", url, err);
          } finally {
            if (prevModule === undefined) {
              try { delete w.module; } catch {}
            } else {
              w.module = prevModule;
            }
          }
        } catch (err) {
          console.warn?.("MapView: OMS fetch error", url, err);
        }
      }
      return null;
    };

    trySequential().then(resolve);
  });
  return _omsPromise;
}

// (opcional) pequeño jitter para puntos con coordenadas idénticas
function jitterPointsIfNeeded(arr: ExplorerPoint[], enabled: boolean, radius = 0.0003) {
  if (!enabled) return arr;
  const byKey = new Map<string, ExplorerPoint[]>();
  arr.forEach(p => {
    const k = `${p.lat?.toFixed(6)},${p.lng?.toFixed(6)}`;
    const a = byKey.get(k) || [];
    a.push(p); byKey.set(k, a);
  });
  const out: ExplorerPoint[] = [];
  const jitterRadius = radius; // ≈25–35 m por 0.0003
  byKey.forEach((group) => {
    if (group.length === 1) { out.push(group[0]); return; }
    // orden estable por account_id para que el jitter no cambie al re-render
    const sorted = [...group].sort((a,b) => (a.account_id < b.account_id ? -1 : a.account_id > b.account_id ? 1 : 0));
    sorted.forEach((p, idx) => {
      const angle = (idx / sorted.length) * 2 * Math.PI;
      out.push({
        ...p,
        lat: (p.lat || 0) + jitterRadius * Math.cos(angle),
        lng: (p.lng || 0) + jitterRadius * Math.sin(angle),
      });
    });
  });
  return out;
}

const EARTH_RADIUS_KM = 6371;
const CLUSTER_RADIUS_KM = 5; // agrupa centros solo dentro de 5 km
// Zoom a partir del cual re-colapsamos el cluster para volver a mostrar el contador
const CLUSTER_RECOLLAPSE_ZOOM = 9;
// Límite superior al hacer zoom al expandir un cluster (evita mega-zoom)
const MAX_CLUSTER_ZOOM = 11;
// Zoom mínimo para asegurar separación visible tras expandir clusters pequeños
const MIN_CLUSTER_ZOOM = 13;

type ManualCluster = {
  id: string;
  center: { lat: number; lng: number };
  points: (ExplorerPoint & { lat: number; lng: number })[];
};

function haversineKm(lat1: number, lng1: number, lat2: number, lng2: number): number {
  const toRad = (deg: number) => (deg * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLng = toRad(lng2 - lng1);
  const a =
    Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) *
    Math.sin(dLng / 2) * Math.sin(dLng / 2);
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  return EARTH_RADIUS_KM * c;
}

function buildManualClusters(points: ExplorerPoint[]): ManualCluster[] {
  const valid = points.filter(
    (p): p is ExplorerPoint & { lat: number; lng: number } =>
      typeof p.lat === "number" && typeof p.lng === "number"
  );

  const clusters: ManualCluster[] = [];
  const visited = new Set<number>();

  for (let i = 0; i < valid.length; i++) {
    if (visited.has(i)) continue;
    const queue = [i];
    visited.add(i);
    const membersIdx: number[] = [];

    while (queue.length) {
      const idx = queue.pop()!;
      membersIdx.push(idx);
      const base = valid[idx];

      for (let j = 0; j < valid.length; j++) {
        if (visited.has(j)) continue;
        const candidate = valid[j];
        if (haversineKm(base.lat, base.lng, candidate.lat, candidate.lng) <= CLUSTER_RADIUS_KM) {
          visited.add(j);
          queue.push(j);
        }
      }
    }

    const members = membersIdx.map((idx) => valid[idx]);
    const center = members.reduce(
      (acc, p) => {
        acc.lat += p.lat;
        acc.lng += p.lng;
        return acc;
      },
      { lat: 0, lng: 0 }
    );
    center.lat /= members.length || 1;
    center.lng /= members.length || 1;

    const id = members
      .map((m) => m.account_id)
      .sort((a, b) => (a < b ? -1 : a > b ? 1 : 0))
      .join("|") || `cluster-${clusters.length}`;

    clusters.push({ id, center, points: members });
  }

  return clusters;
}

function clusterIconSvg(count: number, size = 34) {
  const display = count > 99 ? "99+" : String(count);
  const strokeWidth = 3;
  const radius = Math.floor(size / 2) - strokeWidth;
  const center = Math.floor(size / 2);
  const svg = encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
      <defs>
        <radialGradient id="clusterGradient" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stop-color="#2563eb" stop-opacity="0.95" />
          <stop offset="100%" stop-color="#1d4ed8" stop-opacity="0.95" />
        </radialGradient>
      </defs>
      <circle cx="${center}" cy="${center}" r="${radius}" fill="url(#clusterGradient)" stroke="white" stroke-width="${strokeWidth}" />
      <text x="50%" y="55%" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-weight="600" font-size="${size * 0.38}" fill="white">
        ${display}
      </text>
    </svg>`
  );
  return `data:image/svg+xml;charset=UTF-8,${svg}`;
}

const clusterIconCache = new Map<number, string>();
function getClusterIcon(count: number) {
  if (!clusterIconCache.has(count)) {
    clusterIconCache.set(count, clusterIconSvg(count));
  }
  return clusterIconCache.get(count)!;
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

  // Mapa + Spiderfier (declarado ANTES de cualquier uso)
  const [map, setMap] = useState<any | null>(null);
  // Cache local de detalles por account (evita re-fetch repetido)
  const detailsCacheRef = useRef<Record<string, any>>({});
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [detailsError, setDetailsError] = useState<string | null>(null);
  const [currentDetails, setCurrentDetails] = useState<any | null>(null);
  const spiderfierRef = useRef<any | null>(null);
  const [omsReady, setOmsReady] = useState(false); // ← para saber si OMS está operativo

  // Centro inicial: primer point válido o vecino; no re-centramos en renders posteriores
  const initialCenterCandidate = useMemo(() => {
    const p = (points || []).find((p) => p.lat && p.lng) || (neighborsAll || []).find((p) => p.lat && p.lng);
    return p ? { lat: p.lat!, lng: p.lng! } : { lat: 48.8566, lng: 2.3522 };
  }, [points, neighborsAll]);
  const initialCenterRef = useRef<{ lat: number; lng: number } | null>(null);
  if (!initialCenterRef.current) initialCenterRef.current = initialCenterCandidate;

  // Evita duplicados: quita de neighbors los que ya están en points
  const pointsIds = useMemo(() => new Set(points.map((p) => p.account_id)), [points]);
  const neighborsOnly = useMemo(
    () => (neighborsAll || []).filter((n) => !pointsIds.has(n.account_id)),
    [neighborsAll, pointsIds]
  );

  const renderNeighbors = useMemo(
    () => jitterPointsIfNeeded(neighborsOnly, true, 0.0003),
    [neighborsOnly]
  );

  const manualClusters = useMemo(() => buildManualClusters(points), [points]);
  const singlePoints = useMemo(() => manualClusters.filter((c) => c.points.length === 1).map((c) => c.points[0]), [manualClusters]);
  const multiClusters = useMemo(() => manualClusters.filter((c) => c.points.length > 1), [manualClusters]);
  const [expandedClusterId, setExpandedClusterId] = useState<string | null>(null);
  // zoom actual del mapa (para decidir cuándo re-colapsar)
  const [zoom, setZoom] = useState<number | null>(null);

  const clusterIdByAccount = useMemo(() => {
    const out = new Map<string, string>();
    multiClusters.forEach((cluster) => {
      cluster.points.forEach((p) => out.set(p.account_id, cluster.id));
    });
    return out;
  }, [multiClusters]);

  useEffect(() => {
    if (!expandedClusterId) return;
    if (!multiClusters.some((c) => c.id === expandedClusterId)) {
      setExpandedClusterId(null);
    }
  }, [multiClusters, expandedClusterId]);

  useEffect(() => {
    if (!selectedId) return;
    const cid = clusterIdByAccount.get(selectedId);
    if (!cid) return;
    const cluster = multiClusters.find((c) => c.id === cid);
    if (cluster && cluster.points.length > 1) {
      setExpandedClusterId(cid);
    }
  }, [selectedId, clusterIdByAccount, multiClusters]);

  useEffect(() => {
    if (!highlightedAccountIds || highlightedAccountIds.length === 0) return;
    for (const id of highlightedAccountIds) {
      const cid = clusterIdByAccount.get(id);
      if (!cid) continue;
      const cluster = multiClusters.find((c) => c.id === cid);
      if (cluster && cluster.points.length > 1) {
        setExpandedClusterId(cid);
        break;
      }
    }
  }, [highlightedAccountIds, clusterIdByAccount, multiClusters]);

  useEffect(() => {
    if (!map) return;
    const zoomListener = map.addListener("zoom_changed", () => {
      try {
        const z = typeof map.getZoom === 'function' ? map.getZoom() : null;
        if (z != null) setZoom(z);
        // Re-colapsa solo cuando hacemos zoom-out fuerte
        if (expandedClusterId && z != null && z <= CLUSTER_RECOLLAPSE_ZOOM) {
          setExpandedClusterId(null);
        }
      } catch {}
    });
    return () => {
      if (zoomListener && typeof zoomListener.remove === "function") zoomListener.remove();
    };
  }, [map, expandedClusterId]);

  const singlePointsJittered = useMemo(() => jitterPointsIfNeeded(singlePoints, true, 0.0003), [singlePoints]);
  const expandedCluster = useMemo(() => multiClusters.find((c) => c.id === expandedClusterId) || null, [multiClusters, expandedClusterId]);
  // Más separación al expandir
  const expandedPoints = useMemo(() => (expandedCluster ? jitterPointsIfNeeded(expandedCluster.points, true, 0.0015) : []), [expandedCluster]);
  const expandedIds = useMemo(() => {
    if (!expandedCluster) return new Set<string>();
    return new Set(expandedCluster.points.map((p) => p.account_id));
  }, [expandedCluster]);
  const visiblePoints = useMemo(() => {
    if (!expandedCluster) return singlePointsJittered;
    return [
      ...singlePointsJittered.filter((p) => !expandedIds.has(p.account_id)),
      ...expandedPoints,
    ];
  }, [singlePointsJittered, expandedPoints, expandedIds, expandedCluster]);
  const collapsedClusters = useMemo(() => {
    if (!expandedCluster) return multiClusters;
    return multiClusters.filter((c) => c.id !== expandedCluster.id);
  }, [multiClusters, expandedCluster]);

  const onMarkerClick = useCallback(
    (id: string) => setSelected(id),
    [setSelected]
  );
  const onMapClick = useCallback(() => {
    // Solo deselecciona; no re-colapsar el cluster para que el spiderfier siga visible
    setSelected(null);
  }, [setSelected]);


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
          nearbyDistance: 56,    // píxeles; detecta mejor superposiciones
          circleSpiralSwitchover: 2,
          spiralFootSeparation: 48,
          spiralLengthStart: 44,
          spiralLengthFactor: 6,
          circleFootSeparation: 50,
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
      try {
        const g = (window as any).google;
        // Espera a que el mapa tenga projection (idle) antes de crear OMS
        g.maps.event.addListenerOnce(map, 'idle', () => setupOms(map));
      } catch {
        setupOms(map);
      }
    }

    return () => {
      disposed = true;
      try { spiderfierRef.current?.clearMarkers(); } catch {}
      spiderfierRef.current = null;
      setOmsReady(false);
    };
  }, [map, setSelected]);

  // Helpers para registrar / desregistrar markers con OMS
  // registro de instancias para poder disparar spiderfy programáticamente
  const markersRef = useRef<Map<string, any>>(new Map());
  const spiderfyClusterAfterIdle = useCallback((cluster: ManualCluster) => {
    try {
      const g = (window as any).google;
      if (!g || !map || !omsReady || !spiderfierRef.current) return;
      const ids = cluster.points.map((p) => p.account_id);
      // espera a markers, a idle y a que OMS tenga projection
      const trySpiderfy = () => {
        const haveAll = ids.every((id) => !!markersRef.current.get(id));
        const hasProj = !!(spiderfierRef.current as any)?.projHelper_?.getProjection?.();
        if (!haveAll || !hasProj) { setTimeout(trySpiderfy, 60); return; }
        g.maps.event.addListenerOnce(map, 'idle', () => {
          setTimeout(() => {
            try {
              const first = ids.map((id) => markersRef.current.get(id)).find(Boolean);
              if (first && typeof spiderfierRef.current!.spiderfy === 'function') {
                spiderfierRef.current!.spiderfy(first);
              }
            } catch {}
          }, 0);
        });
      };
      trySpiderfy();
    } catch {}
  }, [map, omsReady]);
  const makeRegisterMarker = (id: string) => (m: any | null) => {
    if (!m) return;
    try { markersRef.current.set(id, m); } catch {}
    try {
      if (spiderfierRef.current) {
        m.set("account_id", id);
        spiderfierRef.current.addMarker(m);
      }
    } catch {}
  };

  const makeUnregisterMarker =
    (id: string) => (m: any | null) => {
      try { markersRef.current.delete(id); } catch {}
      if (!m) return;
      try { spiderfierRef.current?.removeMarker(m); } catch {}
    };

  // Cuando OMS queda listo más tarde, registra todos los markers existentes
  useEffect(() => {
    if (!omsReady || !spiderfierRef.current) return;
    try {
      markersRef.current.forEach((m, id) => {
        try { m.set("account_id", id); spiderfierRef.current!.addMarker(m); } catch {}
      });
    } catch {}
  }, [omsReady]);

  // Cuando cambia la selección (selectedId), intenta cargar los extras del account
  useEffect(() => {
    let alive = true;
    const id = selectedId;
    if (!id) {
      setCurrentDetails(null);
      setDetailsError(null);
      setDetailsLoading(false);
      return;
    }

    // Si ya lo tenemos en caché, úsalo
    const cached = detailsCacheRef.current[id];
    if (cached) {
      setCurrentDetails(cached);
      setDetailsLoading(false);
      setDetailsError(null);
      return;
    }

    const run = async () => {
      setDetailsLoading(true);
      setDetailsError(null);
      try {
        const res = await fetch(`/api/salesforce/explorer/account-extras/${encodeURIComponent(id)}`, { credentials: "include" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const txt = await res.text();
        let json: any = null;
        try { json = txt ? JSON.parse(txt) : {}; } catch (e) { json = { _parse_error: String(e), _body: txt }; }
        if (!alive) return;
        detailsCacheRef.current[id] = json;
        setCurrentDetails(json);
      } catch (e: any) {
        if (!alive) return;
        setDetailsError(e?.message || String(e));
        setCurrentDetails(null);
      } finally {
        if (alive) setDetailsLoading(false);
      }
    };
    run();
    return () => { alive = false; };
  }, [selectedId]);

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
      defaultCenter={initialCenterRef.current!}
      defaultZoom={5}
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
      {renderNeighbors
        .filter((p) => p.lat && p.lng)
        .map((p) => (
          <MarkerF
            key={`nbr-${p.account_id}`}
            position={{ lat: p.lat!, lng: p.lng! }}
            icon={{ url: neighborIconUrl }}
            zIndex={1}
            options={{ cursor: "pointer" }}
            // cuando OMS está listo dejamos el click a OMS; si no, fallback
            onClick={() => onMarkerClick(p.account_id)}
            onLoad={makeRegisterMarker(p.account_id)}
            onUnmount={makeUnregisterMarker(p.account_id)}
          />
        ))}

      {/* Clusters colapsados */}
      {collapsedClusters.map((cluster) => (
        <MarkerF
          key={`cluster-${cluster.id}`}
          position={{ lat: cluster.center.lat, lng: cluster.center.lng }}
          icon={{ url: getClusterIcon(cluster.points.length) }}
          zIndex={45}
          options={{ cursor: "pointer" }}
          onClick={() => {
            setSelected(null);
            setExpandedClusterId(cluster.id);
            if (map) {
              try {
                const g = (window as any).google;
                const bounds = new g.maps.LatLngBounds();
                cluster.points.forEach((p) => bounds.extend({ lat: p.lat, lng: p.lng }));
                map.fitBounds(bounds, 72);
                // Ajusta zoom a un rango visible para separación
                const z = typeof map.getZoom === 'function' ? map.getZoom() : null;
                if (z != null) {
                  const target = Math.max(MIN_CLUSTER_ZOOM, Math.min(z, MAX_CLUSTER_ZOOM));
                  if (target !== z) map.setZoom(target);
                }
                // Centra en el centro geométrico
                if (typeof map.setCenter === 'function') map.setCenter(cluster.center);
              } catch {}
              // fuerza spiderfy tras el ajuste de bounds
              spiderfyClusterAfterIdle(cluster);
            }
          }}
        />
      ))}

      {/* Puntos FILTRADOS */}
      {visiblePoints
        .filter((p) => p.lat && p.lng)
        .map((p) => {
          const isSelected = selectedId === p.account_id;
          const isHighlighted = highlightedSet.has(p.account_id); // <-- FIX
          // Prefer fetched details (cached or current) when available; fallback to p.meta
          const cached = detailsCacheRef.current[p.account_id];
          const detail = (isSelected ? currentDetails : cached) || (p.meta || {});

          // CS flags can come from several places. Prefer fetched details (detail.csContribution),
          // then the bootstrap top-level p.cs, then legacy/compatibility fields under p.meta.
          const csFromDetail = detail?.csContribution;
          const meta = (p.meta || {}) as any;

          const legacyClinical = meta?.csContribution?.Clinical_Site_CS__c || meta?.["extra.CS_Clinical_Site_CS__c"] || meta?.["extra.Clinical_Site_CS__c"] || meta?.["extra.INNODIA_Clinical_Trial_Site__c"];
          const legacyReferral = meta?.csContribution?.Referral_Outreach_Site_Non_CTS__c || meta?.["extra.CS_Referral_Outreach_Site__c"] || meta?.["extra.Referral_Outreach_Site_Non_CTS__c"];
          const legacyDetect = meta?.csContribution?.Elegible_for_DETECT_Site__c || meta?.["extra.CS_Eligible_DETECT__c"] || meta?.["extra.Elegible_for_DETECT_Site__c"];

          const csFlags = csFromDetail
            ? {
                clinical: !!csFromDetail.INNODIA_Clinical_Trial_Site__c,
                referral: !!csFromDetail.Referral_Outreach_Site_Non_CTS__c,
                detect: !!csFromDetail.Elegible_for_DETECT_Site__c,
              }
            : {
                clinical: !!(p.cs?.clinical || legacyClinical),
                detect: !!(p.cs?.detect || legacyDetect),
                referral: !!(p.cs?.referral || legacyReferral),
              };

          // Only warn when absolutely no CS information was present from any source
          if (!csFromDetail && !p.cs && !meta && !(legacyClinical || legacyReferral || legacyDetect)) {
            try { console.warn && console.warn('MapView: marker MISSING cs in bootstrap node', p.account_id, p); } catch (_) {}
          }

          const assignments = Array.isArray(detail.assignments) ? detail.assignments : (Array.isArray(p.meta?.assignments) ? p.meta.assignments : []);
          const ndUnder18 = detail?.newDxUnder18 ?? detail?.opportunity?.new_dx_u18 ?? p.meta?.nd_u18 ?? null;
          const ndOver18 = detail?.newDxOver18 ?? detail?.opportunity?.new_dx_o18 ?? p.meta?.nd_o18 ?? null;
          const piName = detail?.pi?.name ?? detail?.pi_name ?? p.meta?.pi_name ?? null;
          const piEmail = detail?.pi?.email ?? detail?.pi_email ?? p.meta?.pi_email ?? null;
          const piPhone = detail?.pi?.phone ?? detail?.pi_phone ?? p.meta?.pi_phone ?? null;
          const memberName = detail?.member?.name ?? null;
          return (
            <MarkerF
              // include cs flags in key so React will recreate the marker when flags change
              key={`${p.account_id}-${csFlags?.clinical?1:0}-${csFlags?.detect?1:0}-${csFlags?.referral?1:0}`}
              position={{ lat: p.lat!, lng: p.lng! }}
              icon={iconForCs(csFlags)}
              zIndex={isSelected ? 100 : (isHighlighted ? 20 : 10)}
              options={{ cursor: "pointer" }}
              onClick={() => onMarkerClick(p.account_id)}
              onLoad={(m: any) => {
                try { makeRegisterMarker(p.account_id)(m); } catch (_) {}
              }}
              onUnmount={makeUnregisterMarker(p.account_id)}
            >
              {isSelected && (
                  <InfoWindowF
                    position={{ lat: p.lat!, lng: p.lng! }}
                    onCloseClick={() => setSelected(null)}
                    options={{ maxWidth: 640, disableAutoPan: true }}
                  >
                  <div className="text-lg space-y-3" style={{ minWidth: 460, maxWidth: 580 }}>
                    <div>
                      <div className="font-semibold">{p.account_name}</div>
                      <div className="text-xs text-gray-600">{p.city ?? "-"}, {p.country ?? "-"}</div>
                    </div>

                    <div className="text-sm text-gray-700">
                          <div className="font-semibold uppercase tracking-wide text-[11px] text-gray-600 mb-2">CS contribution</div>

                          {/* Badges row: shows colored pills for each CS flag that is true (compact) */}
                          <div className="flex flex-wrap gap-2 mb-2">
                            {csFlags?.referral && (
                              <span
                                className="inline-flex items-center rounded-full px-3 py-0.5 text-sm font-semibold text-white"
                                style={{ backgroundColor: COLOR_REFERRAL }}
                              >
                                Referral & Outreach
                              </span>
                            )}
                            {csFlags?.clinical && (
                              <span
                                className="inline-flex items-center rounded-full px-3 py-0.5 text-sm font-semibold text-white"
                                style={{ backgroundColor: COLOR_INNODIA }}
                              >
                                INNODIA Clinical Trial Site
                              </span>
                            )}
                            {csFlags?.detect && (
                              <span
                                className="inline-flex items-center rounded-full px-3 py-0.5 text-sm font-semibold text-white"
                                style={{ backgroundColor: COLOR_DETECT }}
                              >
                                Eligible for DETECT
                              </span>
                            )}
                            {(!csFlags || (!csFlags.referral && !csFlags.clinical && !csFlags.detect)) && (
                              <span className="inline-flex items-center rounded-full px-3 py-0.5 text-sm font-semibold text-gray-700 bg-gray-200">
                                None
                              </span>
                            )}
                          </div>
                        </div>

                    <div className="text-xs text-gray-700">
                      <div>
                        <span className="font-semibold">Principal Investigator:</span> {piName || "—"}
                        {detailsLoading && isSelected ? (
                          <span className="ml-2 text-xs text-gray-500">(loading…)</span>
                        ) : null}
                      </div>
                      <div className="mt-1">
                        <span className="font-semibold"># ND &gt; 18:</span> {ndOver18 ?? "—"}
                        <span className="ml-2 font-semibold"># ND &lt; 18:</span> {ndUnder18 ?? "—"}
                      </div>
                    </div>

                    <div className="text-xs text-gray-700">
                      <div className="font-semibold">Assignments</div>
                      {assignments.length > 0 ? (
                        <ul className="list-disc list-inside space-y-0.5">
                          {assignments.slice(0, 3).map((a: any) => (
                            <li key={(a && (a.id || a.name || a)) || Math.random()}>
                              {typeof a === "string"
                                ? a
                                : (
                                    `${a.name || a.opportunity_name || a.id}${a.opportunity_name && a.name ? ` (${a.opportunity_name})` : ''}`
                                  )
                              }
                            </li>
                          ))}
                          {assignments.length > 3 ? (
                            <li className="text-[11px] text-gray-500">+{assignments.length - 3} more…</li>
                          ) : null}
                        </ul>
                      ) : (
                        <div className="text-gray-500">—</div>
                      )}
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
      {visiblePoints
        .filter((p) => p.lat && p.lng && highlightedSet.has(p.account_id))
        .map((p) => (
          <MarkerF
            key={`hl-${p.account_id}`}
            position={{ lat: p.lat!, lng: p.lng! }}
            icon={{ url: highlightHaloUrl }}
            zIndex={15}
            onClick={() => onMarkerClick(p.account_id)}
            // No registramos en OMS para no interferir con el clustering de clics
          />
        ))}
    </GoogleMap>
  );
}
