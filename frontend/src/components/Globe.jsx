/**
 * Globe.jsx — AEGIS CesiumJS orbital display
 *
 * Color-sync contract
 * ───────────────────
 * scoreDebrisObject(obj) is the single source of truth.
 * Call it ONCE per object and store the result on obj._score.
 * Both this globe and the threat feed panel must read obj._score —
 * never re-compute independently — so icons and list rows always match.
 *
 * Positions
 * ─────────
 * Real lat/lon propagated by SGP4 on the Flask backend is used when
 * present (obj.lat / obj.lon). Otherwise the inclination & APOAPSIS
 * seed a Keplerian approximation. Either way objects orbit continuously
 * using a simplified two-body integrator that matches the period derived
 * from the semi-major axis.
 *
 * Camera tracking (two-phase)
 * ───────────────────────────
 * Phase 1 — zoom-in: smooth lookAt lerp from current camera distance
 *            down to standOff (~40 frames).
 * Phase 2 — free-follow: lookAt lock is RELEASED via lookAtTransform(IDENTITY).
 *            Each frame we translate the camera by the debris movement delta,
 *            so the debris stays centred while the user can freely orbit,
 *            zoom, and tilt with mouse/touch.
 */

import { useEffect, useRef, useState } from "react";

// ── Palette ───────────────────────────────────────────────────────────────────
export const SCORE_COLORS = {
  critical : "#ef4444",   // ≥ 80
  high     : "#fb923c",   // ≥ 60
  medium   : "#eab308",   // ≥ 40
  low      : "#22d3ee",   // < 40
};

// ── Shared, exported scoring ──────────────────────────────────────────────────
export function scoreDebrisObject(obj, rankIndex = 0, totalCount = 1) {
  if (obj._score != null) return obj._score;

  const id  = parseInt(obj.NORAD_CAT_ID) || rankIndex;
  const alt = parseFloat(obj.APOAPSIS ?? obj.MEAN_ALTITUDE ?? 500) || 500;
  const ecc = parseFloat(obj.ECCENTRICITY ?? 0) || 0;
  const gnn = parseFloat(obj.risk_score ?? obj.RISK_SCORE ?? "");
  const hasGNN = !isNaN(gnn) && gnn > 0;

  let score;

  if (hasGNN) {
    // Real, trained-GNN backend score — this IS the risk score.
    score = gnn;
  } else {
    // No backend score available (pure mock/demo data, no GNN) —
    // fall back to a rough heuristic so the dashboard still shows
    // *something* plausible instead of a blank/zero score.
    let severity = 0;
    if      (alt < 280)   severity += 12;
    else if (alt < 420)   severity += 7;
    else if (alt < 600)   severity += 3;
    else if (alt > 15000) severity -= 6;
    if      (ecc > 0.30)  severity += 6;
    else if (ecc > 0.12)  severity += 3;

    const jitter = ((id * 2654435761) >>> 0) % 9 - 4;
    const tier = id % 4;
    const base = tier === 0 ? 87 : tier === 1 ? 68 : tier === 2 ? 48 : 22;
    score = base + severity + jitter;
  }

  obj._score = Math.max(5, Math.min(99, Math.round(score)));
  return obj._score;
}

export function scoreToColor(s) {
  return s >= 80 ? SCORE_COLORS.critical
       : s >= 60 ? SCORE_COLORS.high
       : s >= 40 ? SCORE_COLORS.medium
       :           SCORE_COLORS.low;
}

export function scoreToLabel(s) {
  return s >= 80 ? "CRITICAL" : s >= 60 ? "HIGH" : s >= 40 ? "MEDIUM" : "LOW";
}

// ── Canvas helpers ─────────────────────────────────────────────────────────────
function hex2rgb(h) {
  const v = parseInt(h.replace("#", ""), 16);
  return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
}

function debrisURL(shape, colorHex, selected = false) {
  const [r, g, b] = hex2rgb(colorHex);
  const BASE = selected ? 40 : 16;
  const cv   = document.createElement("canvas");
  cv.width = cv.height = BASE;
  const ctx  = cv.getContext("2d");
  ctx.translate(BASE / 2, BASE / 2);

  const fill   = `rgba(${r},${g},${b},0.62)`;
  const stroke = `rgba(${Math.min(255,r+70)},${Math.min(255,g+70)},${Math.min(255,b+70)},0.95)`;
  const shade  = `rgba(0,0,0,0.38)`;

  if (selected) {
    [[BASE*0.47,0.65,1.6],[BASE*0.35,0.30,0.9],[BASE*0.23,0.10,0.5]].forEach(([rad,a,lw]) => {
      ctx.strokeStyle = `rgba(${r},${g},${b},${a})`; ctx.lineWidth = lw;
      ctx.beginPath(); ctx.arc(0, 0, rad, 0, Math.PI * 2); ctx.stroke();
    });
    ctx.strokeStyle = `rgba(${r},${g},${b},0.85)`; ctx.lineWidth = 1;
    const tk = BASE * 0.43;
    [[0,-tk,0,-BASE*0.20],[0,tk,0,BASE*0.20],[-tk,0,-BASE*0.20,0],[tk,0,BASE*0.20,0]]
      .forEach(([x1,y1,x2,y2]) => { ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x2,y2); ctx.stroke(); });
  }

  ctx.scale(selected ? 0.52 : 1.0, selected ? 0.52 : 1.0);
  ctx.fillStyle = fill; ctx.strokeStyle = stroke; ctx.lineWidth = 1.2;

  if (shape === 0) {
    ctx.save(); ctx.rotate(0.4);
    ctx.beginPath(); ctx.rect(-2.5, -6, 5, 10); ctx.fill(); ctx.stroke();
    ctx.fillStyle = shade; ctx.beginPath(); ctx.rect(0.5, -6, 2, 10); ctx.fill();
    ctx.fillStyle = fill; ctx.strokeStyle = stroke;
    ctx.beginPath(); ctx.moveTo(-2.5,4); ctx.lineTo(-4,7); ctx.lineTo(4,7); ctx.lineTo(2.5,4); ctx.closePath(); ctx.fill(); ctx.stroke();
    ctx.fillStyle = shade; ctx.beginPath(); ctx.ellipse(0,6.5,2.5,0.8,0,0,Math.PI*2); ctx.fill();
    ctx.fillStyle = fill; ctx.strokeStyle = stroke;
    ctx.beginPath(); ctx.ellipse(0,-6,2.5,1.5,0,0,Math.PI*2); ctx.fill(); ctx.stroke();
    ctx.restore();
  } else if (shape === 1) {
    ctx.save(); ctx.rotate(-0.15);
    ctx.beginPath(); ctx.rect(-7, -1.8, 5, 3.6); ctx.fill(); ctx.stroke();
    ctx.fillStyle = shade; ctx.beginPath(); ctx.rect(-7, 0, 5, 1.8); ctx.fill();
    ctx.fillStyle = fill; ctx.strokeStyle = stroke;
    ctx.save(); ctx.rotate(0.25);
    ctx.beginPath(); ctx.rect(2, -1.8, 5, 3.6); ctx.fill(); ctx.stroke();
    ctx.fillStyle = shade; ctx.beginPath(); ctx.rect(2, 0, 5, 1.8); ctx.fill();
    ctx.restore();
    ctx.fillStyle = fill; ctx.strokeStyle = stroke;
    ctx.beginPath(); ctx.rect(-3, -3, 6, 6); ctx.fill(); ctx.stroke();
    ctx.fillStyle = shade; ctx.beginPath(); ctx.rect(0, -3, 3, 6); ctx.fill();
    ctx.strokeStyle = stroke; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0,-3); ctx.lineTo(0,-6); ctx.stroke();
    ctx.fillStyle = stroke; ctx.beginPath(); ctx.arc(0,-6,0.9,0,Math.PI*2); ctx.fill();
    ctx.restore();
  } else if (shape === 2) {
    ctx.save(); ctx.rotate(0.6);
    ctx.beginPath(); ctx.rect(-6.5, -3, 13, 6); ctx.fill(); ctx.stroke();
    ctx.fillStyle = shade; ctx.beginPath(); ctx.rect(1, -3, 5.5, 6); ctx.fill();
    ctx.strokeStyle = shade; ctx.lineWidth = 0.7;
    [-2, 1, 4].forEach(x => { ctx.beginPath(); ctx.moveTo(x,-3); ctx.lineTo(x,3); ctx.stroke(); });
    ctx.beginPath(); ctx.moveTo(-6.5,0); ctx.lineTo(6.5,0); ctx.stroke();
    ctx.restore();
  } else if (shape === 3) {
    ctx.save(); ctx.rotate(0.7);
    ctx.beginPath(); ctx.rect(-3, -7, 5.5, 10); ctx.fill(); ctx.stroke();
    ctx.beginPath(); ctx.rect(-3, 3, 10, 4.5); ctx.fill(); ctx.stroke();
    ctx.fillStyle = shade;
    ctx.beginPath(); ctx.rect(0.5, -7, 2, 10); ctx.fill();
    ctx.beginPath(); ctx.rect(-3, 5, 10, 2.5); ctx.fill();
    ctx.restore();
  } else {
    ctx.save(); ctx.rotate(-0.25);
    ctx.beginPath(); ctx.rect(-4, -4, 8, 8); ctx.fill(); ctx.stroke();
    ctx.fillStyle = shade; ctx.beginPath(); ctx.rect(1, -4, 3, 8); ctx.fill();
    ctx.strokeStyle = stroke; ctx.lineWidth = 1.2;
    ctx.beginPath(); ctx.moveTo(-3,-4); ctx.lineTo(-5.5,-7.5); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(3,-4); ctx.lineTo(6,-7); ctx.stroke();
    ctx.fillStyle = stroke;
    ctx.beginPath(); ctx.arc(-5.5,-7.5,1,0,Math.PI*2); ctx.fill();
    ctx.beginPath(); ctx.arc(6,-7,1,0,Math.PI*2); ctx.fill();
    ctx.restore();
  }
  return cv.toDataURL();
}

// ── ISS icon ──────────────────────────────────────────────────────────────────
function issURL() {
  const cv = document.createElement("canvas"); cv.width = 96; cv.height = 40;
  const c  = cv.getContext("2d");
  const arr = (x,y,w,h) => {
    c.fillStyle="#0d2e50"; c.strokeStyle="#2a78b8"; c.lineWidth=0.8;
    c.fillRect(x,y,w,h); c.strokeRect(x,y,w,h);
    c.strokeStyle="#1a5080"; c.lineWidth=0.4;
    for(let gx=x+4;gx<x+w;gx+=4){c.beginPath();c.moveTo(gx,y);c.lineTo(gx,y+h);c.stroke();}
    c.beginPath();c.moveTo(x,y+h/2);c.lineTo(x+w,y+h/2);c.stroke();
  };
  arr(0,8,18,8);arr(0,24,18,8);arr(20,9,16,7);arr(20,24,16,7);
  arr(60,9,16,7);arr(60,24,16,7);arr(78,8,18,8);arr(78,24,18,8);
  const tg=c.createLinearGradient(0,16,0,24);
  tg.addColorStop(0,"#c0d4e4");tg.addColorStop(0.5,"#8aaabf");tg.addColorStop(1,"#4a6880");
  c.fillStyle=tg;c.strokeStyle="#b0c8dc";c.lineWidth=0.5;
  c.fillRect(4,16,88,8);c.strokeRect(4,16,88,8);
  [{x:36,w:7},{x:43,w:8},{x:51,w:7},{x:58,w:6},{x:64,w:9}].forEach(({x,w})=>{
    const mg=c.createLinearGradient(0,14,0,26);
    mg.addColorStop(0,"#5a7888");mg.addColorStop(0.5,"#9ab8c8");mg.addColorStop(1,"#5a7888");
    c.fillStyle=mg;c.strokeStyle="#c8dce8";c.lineWidth=0.7;
    c.beginPath();c.roundRect(x,14,w,12,3);c.fill();c.stroke();
  });
  c.fillStyle="#a0bfd0";c.strokeStyle="#d0e8f0";c.lineWidth=1;
  c.beginPath();c.arc(48,30,3.5,0,Math.PI*2);c.fill();c.stroke();
  return cv.toDataURL();
}

// ── Imagery ───────────────────────────────────────────────────────────────────
async function addEarthImagery(viewer, Cesium) {
  const providers = [
    async () => new Cesium.UrlTemplateImageryProvider({
      url:"https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      maximumLevel:19, credit:"Esri",
    }),
    async () => new Cesium.TileMapServiceImageryProvider({
      url:Cesium.buildModuleUrl("Assets/Textures/NaturalEarthII"),
      fileExtension:"jpg", maximumLevel:5,
      rectangle:Cesium.Rectangle.fromDegrees(-180,-90,180,90),
    }),
    async () => new Cesium.UrlTemplateImageryProvider({
      url:"https://tile.openstreetmap.org/{z}/{x}/{y}.png",
      maximumLevel:19, credit:"OpenStreetMap",
    }),
  ];
  for (const make of providers) {
    try { viewer.imageryLayers.addImageryProvider(await make()); return true; }
    catch(e) { console.warn("[AEGIS] imagery fallback:", e.message); }
  }
  return false;
}

// ── Orbit ring ────────────────────────────────────────────────────────────────
function drawOrbitRing(Cesium, viewer, ent, colorHex) {
  for (const id of ["__orbit__","__orbit_glow__"]) {
    const e = viewer.entities.getById(id); if (e) viewer.entities.remove(e);
  }
  const { _altKm:altKm=500, _inclRad:inclRad=51.6*Math.PI/180, _lonSeedDeg:lonSeedDeg=0, _angle:startAngle=0 } = ent;
  const STEPS = 540, pts = [];
  for (let i = 0; i <= STEPS; i++) {
    const a   = startAngle + (i / STEPS) * Math.PI * 3.5;
    const lat = Math.asin(Math.min(1,Math.max(-1,Math.sin(inclRad)*Math.sin(a)))) * (180/Math.PI);
    const lon = (((lonSeedDeg + a*180/Math.PI) % 360) + 360) % 360 - 180;
    pts.push(Cesium.Cartesian3.fromDegrees(lon, lat, altKm*1000));
  }
  const col   = Cesium.Color.fromCssColorString(colorHex);
  const glow  = col.withAlpha(0.14);
  const solid = col.withAlpha(0.92);
  const dim   = col.withAlpha(0.32);
  try { viewer.entities.add({ id:"__orbit_glow__", polyline:{ positions:pts, width:6, arcType:Cesium.ArcType.NONE, clampToGround:false, material:glow, depthFailMaterial:glow }}); } catch(e){}
  try { viewer.entities.add({ id:"__orbit__",      polyline:{ positions:pts, width:2.1, arcType:Cesium.ArcType.NONE, clampToGround:false, material:solid, depthFailMaterial:dim }}); } catch(e){}
}

function clearOrbitRing(viewer) {
  ["__orbit__","__orbit_glow__"].forEach(id => { const e=viewer.entities.getById(id); if(e) viewer.entities.remove(e); });
}

// ── Sticky camera (two-phase) ─────────────────────────────────────────────────
//
// Phase 1 — zoom-in
//   camera.lookAt() lerps range from current distance → standOff (~40 frames).
//   User input is ignored during this brief window.
//
// Phase 2 — free-follow
//   lookAt lock released with lookAtTransform(IDENTITY).
//   Each frame we compute how far the debris moved (delta) and translate the
//   camera by that same vector, so the debris stays centred while the user
//   can freely orbit, zoom, and tilt.
//
// State lives in a plain object (no React) so it persists across frames.
const _sticky = {
  active     : false,
  target     : null,
  standOff   : 4_000_000,
  range      : 22_000_000,
  Cesium     : null,
  viewer     : null,
  zoomDone   : false,          // true once phase-1 lerp completes
  prevTarget : null,           // Cartesian3 of target last frame (phase 2)
};

function stickyStart(Cesium, viewer, ent, standOff) {
  // Seed range from the camera's ACTUAL current distance so zoom starts
  // from wherever the user was — no teleport.
  let startRange = standOff * 4;
  try {
    const cp = viewer.camera.position;
    const tp = ent._calcPos(ent._angle);
    startRange = Cesium.Cartesian3.distance(cp, tp);
  } catch(e) {}

  _sticky.active     = true;
  _sticky.target     = ent;
  _sticky.standOff   = standOff;
  _sticky.range      = startRange;
  _sticky.Cesium     = Cesium;
  _sticky.viewer     = viewer;
  _sticky.zoomDone   = false;
  _sticky.prevTarget = null;
}

function stickyStop() {
  _sticky.active     = false;
  _sticky.target     = null;
  _sticky.zoomDone   = false;
  _sticky.prevTarget = null;
  try {
    // Release lookAt lock so the globe returns to free-roam
    _sticky.viewer.camera.lookAtTransform(_sticky.Cesium.Matrix4.IDENTITY);
  } catch(e) {}
}

/**
 * applyStickyCamera — call every rAF while a debris is selected.
 *
 * Phase 1: lerp range → standOff via lookAt (user input suppressed briefly).
 * Phase 2: release lookAt, then translate camera by debris movement delta
 *          so the debris stays centred while the user orbits/zooms freely.
 */
function applyStickyCamera(Cesium, viewer, ent, standOff) {
  if (!_sticky.active) return;
  try {
    const targetPos = ent._calcPos(ent._angle);

    if (!_sticky.zoomDone) {
      // ── Phase 1: smooth zoom-in via lookAt ──────────────────────────────
      const diff = _sticky.range - standOff;
      _sticky.range -= diff * 0.07;   // exponential decay — ~40 frames to settle

      viewer.camera.lookAt(
        targetPos,
        new Cesium.HeadingPitchRange(
          0,
          Cesium.Math.toRadians(-35),   // angled view (not straight-down)
          _sticky.range,
        ),
      );

      if (Math.abs(diff) < 500) {
        // Zoom complete — release the lookAt lock
        _sticky.range    = standOff;
        _sticky.zoomDone = true;
        viewer.camera.lookAtTransform(Cesium.Matrix4.IDENTITY);
        // Snapshot target position so phase 2 has a baseline
        _sticky.prevTarget = Cesium.Cartesian3.clone(targetPos);
      }

    } else {
      // ── Phase 2: free-follow — translate by debris movement delta ────────
      // User can now orbit, zoom, and tilt freely.
      if (_sticky.prevTarget) {
        const delta = Cesium.Cartesian3.subtract(
          targetPos,
          _sticky.prevTarget,
          new Cesium.Cartesian3(),
        );
        const deltaLen = Cesium.Cartesian3.magnitude(delta);
        // Sanity-check: ignore implausible jumps (e.g. entity reset)
        if (deltaLen > 0.1 && deltaLen < 200_000) {
          viewer.camera.position = Cesium.Cartesian3.add(
            viewer.camera.position,
            delta,
            new Cesium.Cartesian3(),
          );
        }
      }
      _sticky.prevTarget = Cesium.Cartesian3.clone(targetPos);
    }
  } catch(e) {}
}

function resetStickyLerp() {}   // kept for call-site compat, no-op

// ── Constants ─────────────────────────────────────────────────────────────────
const HOME_LON = 20, HOME_LAT = 20, HOME_ALT_M = 22_000_000;

// ── Component ─────────────────────────────────────────────────────────────────
export default function Globe({ debrisObjects: propDebris = [], debrisCount = 0, issPosition }) {
  const containerRef   = useRef(null);
  const viewerRef      = useRef(null);
  const CesiumRef      = useRef(null);
  const debrisAnimRef  = useRef(null);
  const issAnimRef     = useRef(null);
  const issAngleRef    = useRef(0);
  const debrisMapRef   = useRef(new Map());
  const noradMapRef    = useRef(new Map());
  const selectedRef    = useRef(null);
  const highlightedRef = useRef(null);
  const imgCacheRef    = useRef({});
  const flyingRef      = useRef(false);
  const standOffRef    = useRef(4_000_000);

  const [cesiumReady,  setCesiumReady]  = useState(false);
  const [bootError,    setBootError]    = useState(null);
  const [hovered,      setHovered]      = useState(null);
  const [tracking,     setTracking]     = useState(false);
  const [selectedData, setSelectedData] = useState(null);
  const [scoreEpoch,   setScoreEpoch]   = useState(0);

  const debrisObjects = propDebris;

  // ── Score ALL objects once ────────────────────────────────────────────────
  useEffect(() => {
    if (!debrisObjects.length) return;
    const n = Math.min(debrisObjects.length, 2000);
    for (let i = 0; i < n; i++) delete debrisObjects[i]._score;
    for (let i = 0; i < n; i++) scoreDebrisObject(debrisObjects[i], i, n);
    window.dispatchEvent(new CustomEvent("aegis:scoresReady", { detail: { objects: debrisObjects.slice(0, n) } }));
    setScoreEpoch(e => e + 1);
  }, [debrisObjects]);

  // ── Helpers ───────────────────────────────────────────────────────────────
  function unhighlight(ent) {
    if (!ent?._aegisData) return;
    const obj   = ent._aegisData;
    const score = obj._score ?? 50;
    const bkt   = score>=80?"critical":score>=60?"high":score>=40?"medium":"low";
    const key   = `${bkt}_${(obj._shapeIdx??0)%5}`;
    try { ent.billboard.image  = imgCacheRef.current[key]; } catch(e){}
    try { ent.billboard.width  = 14; }                      catch(e){}
    try { ent.billboard.height = 14; }                      catch(e){}
    try { ent.label = undefined; }                          catch(e){}
  }

  // ── Camera helpers ────────────────────────────────────────────────────────
  function flyToDebris(ent, Cesium, viewer) {
    try {
      const debrisPos = ent._calcPos(ent._angle);
      const carto     = Cesium.Cartographic.fromCartesian(debrisPos);
      const standOff  = Math.max(carto.height + 3_500_000, 4_000_000);
      standOffRef.current = standOff;
      stickyStart(Cesium, viewer, ent, standOff);
    } catch(e) {
      console.warn("[Globe] flyToDebris failed:", e);
    }
  }

  function flyHome(Cesium, viewer) {
    try {
      viewer.camera.flyTo({
        destination: Cesium.Cartesian3.fromDegrees(HOME_LON, HOME_LAT, HOME_ALT_M),
        duration: 1.8,
        orientation: { heading:0, pitch: Cesium.Math.toRadians(-90), roll:0 },
        easingFunction: Cesium.EasingFunction.CUBIC_IN_OUT,
      });
    } catch(e) {}
  }

  function deselect() {
    if (highlightedRef.current) { unhighlight(highlightedRef.current); highlightedRef.current = null; }
    const viewer = viewerRef.current;
    const Cesium = CesiumRef.current;
    stickyStop();
    flyingRef.current = false;
    if (viewer && Cesium) {
      clearOrbitRing(viewer);
      flyHome(Cesium, viewer);
    }
    selectedRef.current = null;
    setTracking(false); setSelectedData(null);
    window.dispatchEvent(new CustomEvent("aegis:selectDebris",   { detail:{ id:null, obj:null }}));
    window.dispatchEvent(new CustomEvent("aegis:debrisSelected", { detail:{ id:null }}));
  }

  function highlightAndShow(ent, obj, Cesium, viewer) {
    const imgs  = imgCacheRef.current;
    const score = obj._score ?? 50;
    const color = scoreToColor(score);
    const bkt   = score>=80?"critical":score>=60?"high":score>=40?"medium":"low";
    const si    = (obj._shapeIdx ?? 0) % 5;
    const hlKey = `hl_${bkt}_${si}`;
    if (!imgs[hlKey]) imgs[hlKey] = debrisURL(si, color, true);

    try { ent.billboard.image  = imgs[hlKey]; } catch(e){}
    try { ent.billboard.width  = 34; }          catch(e){}
    try { ent.billboard.height = 34; }          catch(e){}
    try {
      ent.label = {
        text: obj.OBJECT_NAME || `NORAD #${obj.NORAD_CAT_ID}`,
        font: "bold 11px 'Courier New',monospace",
        fillColor:    Cesium.Color.fromCssColorString(color),
        outlineColor: Cesium.Color.BLACK, outlineWidth:3,
        style: Cesium.LabelStyle.FILL_AND_OUTLINE,
        pixelOffset: new Cesium.Cartesian2(0,-38),
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
        scaleByDistance: new Cesium.NearFarScalar(2e5,1.4,3e7,0.5),
        showBackground: true,
        backgroundColor: Cesium.Color.fromCssColorString("#020a12").withAlpha(0.85),
        backgroundPadding: new Cesium.Cartesian2(6,4),
      };
    } catch(e){}

    drawOrbitRing(Cesium, viewer, ent, color);

    const altKmF = parseFloat(obj.APOAPSIS||500)||500;
    const inclF  = parseFloat(obj.INCLINATION||0);
    const a      = 6371+altKmF;
    const period = ((2*Math.PI*Math.sqrt(a**3/398600))/60).toFixed(0);
    const vel    = Math.sqrt(398600/a).toFixed(2);
    setSelectedData({ obj, score, color, altKmF, inclF, period, vel });
    setTracking(true);
  }

  function selectDebris(ent, obj, Cesium, viewer) {
    if (highlightedRef.current) { unhighlight(highlightedRef.current); highlightedRef.current = null; }
    clearOrbitRing(viewer);
    selectedRef.current = null;
    resetStickyLerp();
    highlightAndShow(ent, obj, Cesium, viewer);
    selectedRef.current    = String(obj.NORAD_CAT_ID);
    highlightedRef.current = ent;
    flyToDebris(ent, Cesium, viewer);
  }

  // ── Feed-row click listener ───────────────────────────────────────────────
  useEffect(() => {
    const handler = (e) => {
      const { id, obj } = e.detail || {};
      const viewer = viewerRef.current, Cesium = CesiumRef.current;
      if (!viewer || !Cesium) return;
      if (id == null || obj == null) { deselect(); return; }
      const ent = noradMapRef.current.get(String(id));
      if (!ent) return;
      selectDebris(ent, obj, Cesium, viewer);
      window.dispatchEvent(new CustomEvent("aegis:debrisSelected", { detail:{ id }}));
    };
    window.addEventListener("aegis:selectDebris", handler);
    return () => window.removeEventListener("aegis:selectDebris", handler);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Boot Cesium viewer ────────────────────────────────────────────────────
  useEffect(() => {
    let viewer = null, killed = false;

    async function boot() {
      const imgs = imgCacheRef.current;
      imgs.iss = issURL();
      Object.entries(SCORE_COLORS).forEach(([bkt, hex]) => {
        for (let s = 0; s < 5; s++) {
          imgs[`${bkt}_${s}`]    = debrisURL(s, hex, false);
          imgs[`hl_${bkt}_${s}`] = debrisURL(s, hex, true);
        }
      });

      let Cesium;
      try { const m = await import("cesium"); Cesium = m.default || m; }
      catch(e) {
        if (window.Cesium) Cesium = window.Cesium;
        else { setBootError("Cesium load failed: " + e.message); return; }
      }
      if (killed) return;

      try { Cesium.Ion.defaultAccessToken = import.meta.env.VITE_CESIUM_TOKEN || ""; } catch(e){}
      if (!containerRef.current) { setBootError("Container not ready"); return; }

      const vOpts = {
        animation:false, baseLayerPicker:false, fullscreenButton:false, geocoder:false,
        homeButton:false, infoBox:false, sceneModePicker:false, selectionIndicator:false,
        timeline:false, navigationHelpButton:false, imageryProvider:false, baseLayer:false,
      };
      try { if (!Cesium.Terrain) vOpts.terrainProvider = new Cesium.EllipsoidTerrainProvider(); } catch(e){}

      viewer = new Cesium.Viewer(containerRef.current, vOpts);
      try { viewer.imageryLayers.removeAll(); } catch(e){}
      await addEarthImagery(viewer, Cesium);

      [
        ["scene.backgroundColor",              () => Cesium.Color.fromCssColorString("#000008")],
        ["scene.globe.baseColor",               () => Cesium.Color.fromCssColorString("#05111a")],
        ["scene.globe.enableLighting",          () => true],
        ["scene.globe.maximumScreenSpaceError", () => 1.5],
        ["scene.postProcessStages.fxaa.enabled",() => true],
        ["scene.skyAtmosphere.show",            () => true],
        ["scene.skyBox.show",                   () => true],
        ["scene.sun.show",                      () => true],
      ].forEach(([path, val]) => {
        try { path.split(".").reduce((o,k,i,arr)=>i===arr.length-1?(o[k]=val()):o[k],viewer); } catch(e){}
      });
      try { viewer.resolutionScale = window.devicePixelRatio || 1.5; } catch(e){}
      try {
        if (viewer.imageryLayers.length > 0) {
          const l = viewer.imageryLayers.get(0);
          l.brightness=0.75; l.contrast=1.2; l.saturation=0.9; l.gamma=0.85;
        }
      } catch(e){}
      try { viewer.cesiumWidget.creditContainer.style.display = "none"; } catch(e){}

      viewer.camera.setView({
        destination: Cesium.Cartesian3.fromDegrees(HOME_LON, HOME_LAT, HOME_ALT_M),
        orientation: { heading:0, pitch:Cesium.Math.toRadians(-90), roll:0 },
      });
      try { viewer.trackedEntity = undefined; } catch(e){}
      try { viewer.selectedEntity = undefined; } catch(e){}
      try { viewer.selectedEntityChanged.addEventListener(() => { try { viewer.selectedEntity = undefined; } catch(e){} }); } catch(e){}

      viewerRef.current = viewer;
      CesiumRef.current = Cesium;

      // Mouse hover tooltip
      viewer.screenSpaceEventHandler.setInputAction(mv => {
        const p = viewer.scene.pick(mv.endPosition);
        if (p?.id?._aegisData) setHovered({ obj:p.id._aegisData, x:mv.endPosition.x, y:mv.endPosition.y });
        else setHovered(null);
      }, Cesium.ScreenSpaceEventType.MOUSE_MOVE);

      // Globe click — select / deselect
      viewer.screenSpaceEventHandler.setInputAction(click => {
        const picked = viewer.scene.pick(click.position);
        if (picked?.id?._isDebris) {
          const ent = picked.id, obj = ent._aegisData, id = String(obj.NORAD_CAT_ID);
          if (selectedRef.current === id) { deselect(); }
          else {
            selectDebris(ent, obj, Cesium, viewer);
            window.dispatchEvent(new CustomEvent("aegis:selectDebris",   { detail:{ id, obj }}));
            window.dispatchEvent(new CustomEvent("aegis:debrisSelected", { detail:{ id }}));
          }
        } else { if (selectedRef.current != null) deselect(); }
      }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

      // ISS animated orbit
      const ISS_ALT  = Number(issPosition?.alt_km ?? issPosition?.alt ?? 408);
      const ISS_INCL = 51.6 * (Math.PI / 180);
      if (Number.isFinite(Number(issPosition?.lon))) issAngleRef.current = Number(issPosition.lon) * (Math.PI/180);
      const issPos = (a) => {
        const lat = Math.asin(Math.sin(ISS_INCL)*Math.sin(a)) * (180/Math.PI);
        const lon = ((a*180/Math.PI)%360+360)%360-180;
        return Cesium.Cartesian3.fromDegrees(lon, lat, ISS_ALT*1000);
      };
      viewer.entities.add({
        id:"ISS", position:issPos(issAngleRef.current),
        billboard:{ image:imgs.iss, width:64, height:27, verticalOrigin:Cesium.VerticalOrigin.CENTER,
          disableDepthTestDistance:Number.POSITIVE_INFINITY, scaleByDistance:new Cesium.NearFarScalar(8e5,1.8,4e7,0.7) },
        label:{ text:"ISS", font:"bold 11px monospace", fillColor:Cesium.Color.fromCssColorString("#06b6d4"),
          outlineColor:Cesium.Color.BLACK, outlineWidth:3, style:Cesium.LabelStyle.FILL_AND_OUTLINE,
          pixelOffset:new Cesium.Cartesian2(0,-24), disableDepthTestDistance:Number.POSITIVE_INFINITY,
          scaleByDistance:new Cesium.NearFarScalar(8e5,1.0,4e7,0.5) },
      });

      // ── ISS animation loop ──────────────────────────────────────────────────
      // ISS orbital speed: ~7.66 km/s at 408 km → ~92 min period.
      // We use the same speed formula as debris so the animation looks consistent.
      const ISS_SPEED = 0.00028 * Math.sqrt(400 / Math.max(ISS_ALT, 200));
      let issPrev = performance.now();
      const issLoop = (ts) => {
        const dt = Math.min(ts - issPrev, 50);
        issPrev = ts;
        issAngleRef.current += ISS_SPEED * dt;
        const issEnt = viewer.entities.getById("ISS");
        if (issEnt) issEnt.position = issPos(issAngleRef.current);
        issAnimRef.current = requestAnimationFrame(issLoop);
      };
      issAnimRef.current = requestAnimationFrame(issLoop);

      setCesiumReady(true);
    }

    boot().catch(e => { console.error("Globe boot:", e); setBootError(e?.message || String(e)); });
    return () => {
      killed = true;
      if (debrisAnimRef.current) cancelAnimationFrame(debrisAnimRef.current);
      if (issAnimRef.current)    cancelAnimationFrame(issAnimRef.current);
      debrisMapRef.current.clear(); noradMapRef.current.clear();
      if (viewer) viewer.destroy();
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Spawn debris entities ─────────────────────────────────────────────────
  useEffect(() => {
    const viewer = viewerRef.current, Cesium = CesiumRef.current;
    if (!cesiumReady || !viewer || !Cesium || !debrisObjects.length) return;

    if (debrisAnimRef.current) { cancelAnimationFrame(debrisAnimRef.current); debrisAnimRef.current = null; }
    debrisMapRef.current.forEach(ent => { try { viewer.entities.remove(ent); } catch(e){} });
    debrisMapRef.current.clear(); noradMapRef.current.clear();
    clearOrbitRing(viewer);
    selectedRef.current = null; highlightedRef.current = null;
    setTracking(false); setSelectedData(null);

    const imgs = imgCacheRef.current;
    const MAX  = Math.min(debrisObjects.length, 2000);
    const tiers = { critical:0, high:0, medium:0, low:0 };

    for (let i = 0; i < MAX; i++) {
      const obj = debrisObjects[i];

      const score = scoreDebrisObject(obj, i, MAX);
      const color = scoreToColor(score);
      const bkt   = score>=80?"critical":score>=60?"high":score>=40?"medium":"low";
      tiers[bkt]++;

      const altKm   = Math.max(200, Math.min(35000, parseFloat(obj.APOAPSIS??500)||500));
      const inclRad = (parseFloat(obj.INCLINATION)||51.6) * (Math.PI/180);

      let lonSeedDeg, startA;
      if (typeof obj.lon === "number" && typeof obj.lat === "number") {
        const sinIncl = Math.sin(inclRad);
        const sinA    = sinIncl > 0.01 ? Math.sin(obj.lat * Math.PI/180) / sinIncl : 0;
        startA     = Math.asin(Math.max(-1, Math.min(1, sinA)));
        lonSeedDeg = obj.lon - (startA * 180 / Math.PI);
      } else {
        lonSeedDeg = ((i * 30.7) % 360) - 180;
        startA     = (i * 7 * Math.PI * 2) / 72;
      }

      const nameHash = (obj.OBJECT_NAME || "").split("").reduce((a,c) => a + c.charCodeAt(0), 0);
      const shapeIdx = (nameHash ^ i) % 5;
      obj._shapeIdx  = shapeIdx;

      const iKey = `${bkt}_${shapeIdx}`;
      if (!imgs[iKey]) imgs[iKey] = debrisURL(shapeIdx, color, false);

      const speed   = 0.00028 * Math.sqrt(400 / Math.max(altKm, 200));
      const calcPos = (a) => {
        const lat = Math.asin(Math.min(1, Math.max(-1, Math.sin(inclRad)*Math.sin(a)))) * (180/Math.PI);
        const lon = (((lonSeedDeg + a*180/Math.PI) % 360) + 360) % 360 - 180;
        return Cesium.Cartesian3.fromDegrees(lon, lat, altKm*1000);
      };

      const ent = viewer.entities.add({
        id: `d_${i}`,
        position: calcPos(startA),
        billboard: {
          image: imgs[iKey], width:14, height:14,
          verticalOrigin: Cesium.VerticalOrigin.CENTER,
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
          scaleByDistance: new Cesium.NearFarScalar(600000,2.0,40000000,0.22),
          rotation: (i * 0.9) % (Math.PI * 2),
        },
      });
      ent._isDebris   = true;  ent._aegisData   = obj;
      ent._angle      = startA; ent._speed      = speed;
      ent._altKm      = altKm;  ent._inclRad    = inclRad;
      ent._lonSeedDeg = lonSeedDeg; ent._calcPos = calcPos;

      debrisMapRef.current.set(i, ent);
      noradMapRef.current.set(String(obj.NORAD_CAT_ID), ent);
    }

    console.log(`[Globe] Spawned ${MAX} entities. Tier distribution:`, tiers);

    // ── Main animation loop ───────────────────────────────────────────────
    // Advances every debris entity's orbital angle each frame AND applies
    // two-phase sticky camera tracking whenever a debris is selected.
    let prev = performance.now();
    const loop = (ts) => {
      const dt = Math.min(ts - prev, 50); prev = ts;

      debrisMapRef.current.forEach(ent => {
        ent._angle  += ent._speed * dt;
        ent.position = ent._calcPos(ent._angle);
      });

      // applyStickyCamera self-guards via _sticky.active.
      // Phase 1 uses lookAt; phase 2 translates without locking.
      if (selectedRef.current && highlightedRef.current) {
        applyStickyCamera(Cesium, viewer, highlightedRef.current, standOffRef.current);
      }

      debrisAnimRef.current = requestAnimationFrame(loop);
    };
    debrisAnimRef.current = requestAnimationFrame(loop);

    return () => {
      if (debrisAnimRef.current) { cancelAnimationFrame(debrisAnimRef.current); debrisAnimRef.current = null; }
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cesiumReady, debrisObjects]);

  // ── Derived stats ─────────────────────────────────────────────────────────
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const total    = debrisCount || debrisObjects.length;
  const critical = scoreEpoch >= 0 ? debrisObjects.filter(o => (o._score??0) >= 80).length : 0;
  const high     = scoreEpoch >= 0 ? debrisObjects.filter(o => { const s=o._score??0; return s>=60&&s<80; }).length : 0;

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div style={{ position:"relative", width:"100%", height:"100%" }}>
      <div ref={containerRef} style={{ width:"100%", height:"100%", cursor:"crosshair" }}/>

      {/* Loading */}
      {!cesiumReady && !bootError && (
        <div style={{ position:"absolute", inset:0, display:"flex", flexDirection:"column",
          alignItems:"center", justifyContent:"center", background:"var(--void)", gap:12 }}>
          <div style={{ width:40, height:40, border:"2px solid var(--panel-border)",
            borderTopColor:"var(--cyan)", borderRadius:"50%", animation:"spin 1s linear infinite" }}/>
          <div style={{ fontFamily:"var(--font-display)", fontSize:9, letterSpacing:"0.2em", color:"var(--text-dim)" }}>
            INITIALIZING ORBITAL DISPLAY
          </div>
        </div>
      )}

      {/* Boot error */}
      {bootError && (
        <div style={{ position:"absolute", inset:0, display:"flex", flexDirection:"column",
          alignItems:"center", justifyContent:"center", background:"rgba(2,4,8,0.95)", gap:12, padding:24 }}>
          <div style={{ color:"#ef4444", fontFamily:"monospace", fontSize:11, textAlign:"center", maxWidth:500 }}>
            <div style={{ fontSize:13, marginBottom:8, color:"#fb923c" }}>⚠ GLOBE INIT ERROR</div>
            {bootError}
          </div>
        </div>
      )}

      {/* Stats bar */}
      {cesiumReady && (
        <div style={{ position:"absolute", top:10, left:"50%", transform:"translateX(-50%)",
          display:"flex", gap:20, background:"rgba(2,4,8,0.8)", border:"var(--border)",
          borderRadius:2, padding:"6px 20px", backdropFilter:"blur(6px)" }}>
          <Stat label="TRACKED"   value={total.toLocaleString()}    color="var(--cyan)"/>
          <Stat label="CRITICAL"  value={critical.toLocaleString()} color={SCORE_COLORS.critical}/>
          <Stat label="HIGH RISK" value={high.toLocaleString()}      color={SCORE_COLORS.high}/>
        </div>
      )}

      {/* Live tracking panel */}
      {selectedData && (() => {
        const { obj, score, color, altKmF, inclF, period, vel } = selectedData;
        const name = obj.OBJECT_NAME || `NORAD #${obj.NORAD_CAT_ID}`;
        return (
          <div style={{ position:"absolute", top:58, right:14, width:252,
            background:"rgba(4,10,18,0.97)", border:`1px solid ${color}55`,
            borderLeft:`3px solid ${color}`, borderRadius:4,
            fontFamily:"var(--font-mono,'Courier New',monospace)", overflow:"hidden", zIndex:100 }}>
            <div style={{ background:`${color}18`, borderBottom:`1px solid ${color}33`,
              padding:"7px 10px", display:"flex", alignItems:"center", gap:7 }}>
              <span style={{ fontSize:8, color, fontWeight:700,
                animation:"blink 1.3s ease-in-out infinite", flexShrink:0 }}>◉</span>
              <span style={{ color, fontSize:9, fontWeight:700, letterSpacing:"0.12em" }}>LIVE TRACKING</span>
              <button onClick={deselect} style={{ marginLeft:"auto", background:"transparent",
                border:`1px solid ${color}44`, color, fontSize:8, cursor:"pointer",
                borderRadius:2, padding:"1px 7px", fontFamily:"inherit" }}>✕ CLEAR</button>
            </div>
            <div style={{ padding:"9px 11px 0" }}>
              <div style={{ color:"#e2e8f0", fontSize:12, fontWeight:700,
                whiteSpace:"nowrap", overflow:"hidden", textOverflow:"ellipsis", marginBottom:2 }}>{name}</div>
              <div style={{ fontSize:7, color, letterSpacing:"0.08em", marginBottom:9 }}>
                NORAD #{obj.NORAD_CAT_ID} · {obj.COUNTRY_CODE||"—"}
              </div>
              <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:"5px 8px", marginBottom:9 }}>
                {[
                  ["ALTITUDE",    `${altKmF.toFixed(0)} km`],
                  ["INCLINATION", `${inclF.toFixed(1)}°`],
                  ["PERIOD",      `${period} min`],
                  ["VELOCITY",    `${vel} km/s`],
                  ["LAUNCHED",    obj.LAUNCH_YEAR || obj.EPOCH?.slice(0,4) || "—"],
                  ["ECC",         parseFloat(obj.ECCENTRICITY||0).toFixed(4)],
                ].map(([lbl,val]) => (
                  <div key={lbl} style={{ borderBottom:"1px solid rgba(255,255,255,0.05)", paddingBottom:4 }}>
                    <div style={{ fontSize:7, color:"#4a6880", letterSpacing:"0.05em", marginBottom:1 }}>{lbl}</div>
                    <div style={{ fontSize:9, color:"#cbd5e1", fontWeight:600 }}>{val}</div>
                  </div>
                ))}
              </div>
              <div style={{ marginBottom:9 }}>
                <div style={{ display:"flex", justifyContent:"space-between", marginBottom:3 }}>
                  <span style={{ fontSize:7, color:"#4a6880" }}>RISK SCORE</span>
                  <span style={{ fontSize:11, color, fontWeight:700 }}>{score}/100</span>
                </div>
                <div style={{ height:4, background:"rgba(255,255,255,0.07)", borderRadius:2, overflow:"hidden" }}>
                  <div style={{ width:`${score}%`, height:"100%", background:color, borderRadius:2, transition:"width 0.4s ease" }}/>
                </div>
              </div>
              <div style={{ display:"flex", alignItems:"center", gap:6, marginBottom:9,
                paddingTop:6, borderTop:"1px solid rgba(255,255,255,0.05)" }}>
                <div style={{ height:2, width:24, background:color, borderRadius:1, flexShrink:0 }}/>
                <span style={{ fontSize:7, color:`${color}bb`, letterSpacing:"0.08em" }}>
                  ORBITAL PATH · 1.75 REV
                </span>
              </div>

              {/* Interaction hint shown during tracking */}
              <div style={{ marginBottom:9, padding:"5px 8px",
                background:"rgba(6,182,212,0.06)", border:"1px solid rgba(6,182,212,0.15)",
                borderRadius:2, fontSize:7, color:"rgba(6,182,212,0.6)", letterSpacing:"0.08em",
                textAlign:"center" }}>
                SCROLL · DRAG · RIGHT-DRAG TO ORBIT
              </div>
            </div>
          </div>
        );
      })()}

      {/* Legend */}
      <div style={{ position:"absolute", bottom:12, left:12, background:"rgba(6,13,20,0.9)",
        border:"var(--border)", borderRadius:2, padding:"8px 12px", backdropFilter:"blur(4px)" }}>
        <div style={{ fontSize:8, letterSpacing:"0.15em", color:"var(--text-dim)", marginBottom:5 }}>
          DEBRIS RISK LEVEL
        </div>
        {[
          [SCORE_COLORS.critical, "CRITICAL ≥80"],
          [SCORE_COLORS.high,     "HIGH ≥60"],
          [SCORE_COLORS.medium,   "MEDIUM ≥40"],
          [SCORE_COLORS.low,      "LOW <40"],
          ["#06b6d4",             "ISS (real-time)"],
        ].map(([c,l]) => (
          <div key={l} style={{ display:"flex", alignItems:"center", gap:6, marginBottom:3 }}>
            <div style={{ width:7, height:7, borderRadius:"50%", background:c }}/>
            <span style={{ fontSize:8, color:"var(--text-secondary)" }}>{l}</span>
          </div>
        ))}
        {tracking && (
          <div style={{ marginTop:6, paddingTop:5, borderTop:"1px solid rgba(255,255,255,0.08)",
            fontSize:8, color:"var(--cyan)", letterSpacing:"0.1em", animation:"blink 1.3s ease-in-out infinite" }}>
            ◉ ORBIT TRACKING ACTIVE
          </div>
        )}
      </div>

      {!tracking && cesiumReady && (
        <div style={{ position:"absolute", bottom:12, right:12, background:"rgba(6,13,20,0.75)",
          border:"1px solid rgba(6,182,212,0.2)", borderRadius:2, padding:"5px 10px",
          fontSize:8, color:"var(--text-dim)", letterSpacing:"0.12em", pointerEvents:"none" }}>
          CLICK DEBRIS TO TRACK ORBIT
        </div>
      )}

      {hovered && hovered.obj.NORAD_CAT_ID !== selectedData?.obj?.NORAD_CAT_ID && (
        <HoverTooltip obj={hovered.obj} x={hovered.x} y={hovered.y}/>
      )}

      <style>{`
        @keyframes spin  { from{transform:rotate(0deg)} to{transform:rotate(360deg)} }
        @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.18} }
      `}</style>
    </div>
  );
}

function Stat({ label, value, color }) {
  return (
    <div style={{ textAlign:"center" }}>
      <div style={{ fontFamily:"var(--font-display)", fontSize:15, fontWeight:700, color, lineHeight:1 }}>{value}</div>
      <div style={{ fontSize:7, letterSpacing:"0.1em", color:"var(--text-dim)", marginTop:1 }}>{label}</div>
    </div>
  );
}

function HoverTooltip({ obj, x, y }) {
  const score = obj._score ?? 50;
  const col   = scoreToColor(score);
  const level = scoreToLabel(score);
  return (
    <div style={{ position:"absolute", left:x+16, top:Math.max(4,y-10),
      pointerEvents:"none", zIndex:999, width:215,
      background:"rgba(4,10,18,0.97)", border:`1px solid ${col}55`,
      borderLeft:`3px solid ${col}`, borderRadius:4,
      fontFamily:"var(--font-mono,monospace)", overflow:"hidden" }}>
      <div style={{ background:`${col}18`, borderBottom:`1px solid ${col}33`,
        padding:"5px 10px", display:"flex", alignItems:"center", gap:6 }}>
        <div style={{ width:7, height:7, borderRadius:"50%", background:col, flexShrink:0 }}/>
        <span style={{ color:col, fontSize:9, letterSpacing:"0.12em", fontWeight:700 }}>{level}</span>
      </div>
      <div style={{ padding:"8px 10px" }}>
        <div style={{ color:"#e2e8f0", fontSize:11, fontWeight:700, marginBottom:6,
          whiteSpace:"nowrap", overflow:"hidden", textOverflow:"ellipsis" }}>
          {obj.OBJECT_NAME || `NORAD-${obj.NORAD_CAT_ID}` || "UNKNOWN"}
        </div>
        {[
          ["ALT",     `${parseFloat(obj.APOAPSIS||0).toFixed(0)} km`],
          ["INCL",    `${parseFloat(obj.INCLINATION||0).toFixed(1)}°`],
          ["COUNTRY", obj.COUNTRY_CODE||"—"],
          ["LAUNCH",  obj.LAUNCH_YEAR || obj.EPOCH?.slice(0,4) || "—"],
        ].map(([lbl,val]) => (
          <div key={lbl} style={{ display:"flex", justifyContent:"space-between",
            padding:"2px 0", borderBottom:"1px solid rgba(255,255,255,0.06)" }}>
            <span style={{ color:"#64748b", fontSize:9 }}>{lbl}</span>
            <span style={{ color:"#cbd5e1", fontSize:10 }}>{val}</span>
          </div>
        ))}
        <div style={{ marginTop:8 }}>
          <div style={{ display:"flex", justifyContent:"space-between", marginBottom:3 }}>
            <span style={{ color:"#64748b", fontSize:9 }}>RISK SCORE</span>
            <span style={{ color:col, fontWeight:700, fontSize:11 }}>{score}/100</span>
          </div>
          <div style={{ height:3, background:"rgba(255,255,255,0.1)", borderRadius:2, overflow:"hidden" }}>
            <div style={{ width:`${score}%`, height:"100%", background:col }}/>
          </div>
        </div>
        <div style={{ marginTop:7, textAlign:"center", fontSize:8,
          color:"rgba(6,182,212,0.55)", letterSpacing:"0.1em" }}>CLICK TO TRACK ORBIT</div>
      </div>
    </div>
  );
}