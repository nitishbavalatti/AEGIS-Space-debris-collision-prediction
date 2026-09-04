import { useState, useEffect, useRef } from "react";

// ── Import shared scoring from Globe so colors ALWAYS match ──────────────────
// Globe.jsx exports these and writes obj._score on every debris object before
// firing "aegis:scoresReady".  We never compute scores here independently.
import { scoreDebrisObject, scoreToColor, scoreToLabel } from "./Globe";

// ── Fallback mock data (only used when no real data arrives) ─────────────────
const MOCK_DEBRIS = [
  { NORAD_CAT_ID:"25544", OBJECT_NAME:"ISS DEB (BOLT)",         APOAPSIS:408,  INCLINATION:51.6, ECCENTRICITY:0.0003, COUNTRY_CODE:"US",  LAUNCH_YEAR:1998 },
  { NORAD_CAT_ID:"41332", OBJECT_NAME:"COSMOS 2504 DEB",        APOAPSIS:385,  INCLINATION:64.9, ECCENTRICITY:0.012,  COUNTRY_CODE:"CIS", LAUNCH_YEAR:2015 },
  { NORAD_CAT_ID:"38778", OBJECT_NAME:"FENGYUN 1C DEB",         APOAPSIS:841,  INCLINATION:98.8, ECCENTRICITY:0.003,  COUNTRY_CODE:"PRC", LAUNCH_YEAR:2007 },
  { NORAD_CAT_ID:"22675", OBJECT_NAME:"IRIDIUM 33 DEB",         APOAPSIS:776,  INCLINATION:86.4, ECCENTRICITY:0.008,  COUNTRY_CODE:"US",  LAUNCH_YEAR:1997 },
  { NORAD_CAT_ID:"36036", OBJECT_NAME:"SL-16 R/B DEB",          APOAPSIS:320,  INCLINATION:71.0, ECCENTRICITY:0.021,  COUNTRY_CODE:"CIS", LAUNCH_YEAR:1993 },
  { NORAD_CAT_ID:"43641", OBJECT_NAME:"BREEZE-M DEB",           APOAPSIS:290,  INCLINATION:48.9, ECCENTRICITY:0.031,  COUNTRY_CODE:"CIS", LAUNCH_YEAR:2018 },
  { NORAD_CAT_ID:"19046", OBJECT_NAME:"COSMOS 1408 DEB",        APOAPSIS:495,  INCLINATION:82.5, ECCENTRICITY:0.005,  COUNTRY_CODE:"CIS", LAUNCH_YEAR:1982 },
  { NORAD_CAT_ID:"31595", OBJECT_NAME:"H-2A R/B DEB",           APOAPSIS:602,  INCLINATION:98.1, ECCENTRICITY:0.002,  COUNTRY_CODE:"JPN", LAUNCH_YEAR:2007 },
  { NORAD_CAT_ID:"28903", OBJECT_NAME:"MICROSAT-R DEB",         APOAPSIS:447,  INCLINATION:97.4, ECCENTRICITY:0.007,  COUNTRY_CODE:"IND", LAUNCH_YEAR:2019 },
  { NORAD_CAT_ID:"41240", OBJECT_NAME:"TITAN 3C TRANSTAGE DEB", APOAPSIS:330,  INCLINATION:33.1, ECCENTRICITY:0.019,  COUNTRY_CODE:"US",  LAUNCH_YEAR:1968 },
];

export default function ThreatFeed({ topThreats, loadMoreDebris }) {
  const source = topThreats?.length > 0 ? topThreats : MOCK_DEBRIS;

  const [hoveredId,   setHoveredId]   = useState(null);
  const [selectedId,  setSelectedId]  = useState(null);
  // Bump this counter whenever Globe fires aegis:scoresReady so we re-render
  // with the freshly written obj._score values
  const [scoreEpoch,  setScoreEpoch]  = useState(0);
  const rowRefs = useRef({});

  // ── Re-render when Globe finishes scoring objects ─────────────────────────
  useEffect(() => {
    const handler = () => setScoreEpoch(n => n + 1);
    window.addEventListener("aegis:scoresReady", handler);
    return () => window.removeEventListener("aegis:scoresReady", handler);
  }, []);

  // ── Globe → feed selection sync ───────────────────────────────────────────
  useEffect(() => {
    const handler = (e) => {
      const id = e.detail?.id ?? null;
      setSelectedId(id);
      if (id != null) {
        setTimeout(() => {
          rowRefs.current[id]?.scrollIntoView({ behavior:"smooth", block:"nearest" });
        }, 80);
      }
    };
    window.addEventListener("aegis:debrisSelected", handler);
    return () => window.removeEventListener("aegis:debrisSelected", handler);
  }, []);

  // ── Click handler ─────────────────────────────────────────────────────────
  function select(obj) {
    const id   = obj?.NORAD_CAT_ID ?? null;
    const next = selectedId === id ? null : id;
    setSelectedId(next);
    window.dispatchEvent(new CustomEvent("aegis:selectDebris", {
      detail: { id: next, obj: next != null ? obj : null },
    }));
  }

  // ── Build sorted rows ─────────────────────────────────────────────────────
  // scoreEpoch is in the render path so React re-reads obj._score after Globe writes it.
  const rows = source
    .map((obj, i) => {
      // Use Globe's cached score if available (obj._score), otherwise compute
      // with the same shared function so colours are guaranteed identical.
      const score = obj._score != null
        ? obj._score
        : scoreDebrisObject(obj, i, source.length);
      const level = scoreToLabel(score);
      const color = scoreToColor(score);
      return { obj, score, level, color };
    })
    .sort((a, b) => b.score - a.score);

  return (
    <div className="panel" style={{ height:"100%", display:"flex", flexDirection:"column", overflow:"hidden" }}>

      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <div className="panel-header">
        <div className="status-dot red" />
        <span className="panel-title">Active Threat Feed</span>
        <span style={{ marginLeft:"auto", fontFamily:"var(--font-mono)", fontSize:9, color:"var(--text-dim)" }}>
          TOP {rows.length} OBJECTS
        </span>
      </div>

      {/* ── Scrollable list ─────────────────────────────────────────────────── */}
      <div style={{ flex:1, overflowY:"auto", padding:"2px 0" }}>
        {rows.length === 0 ? (
          <div style={{ padding:16, textAlign:"center", color:"var(--text-dim)", fontSize:9 }}>
            AWAITING DATA STREAM…
          </div>
        ) : (
          rows.map(({ obj, score, level, color }, i) => {
            const id    = obj.NORAD_CAT_ID;
            const isHov = hoveredId  === id;
            const isSel = selectedId === id;
            const c     = color;

            return (
              <div
                key={id || i}
                ref={el => { rowRefs.current[id] = el; }}
                onClick={() => select(obj)}
                onMouseEnter={() => setHoveredId(id)}
                onMouseLeave={() => setHoveredId(null)}
                style={{
                  padding: "6px 12px",
                  borderBottom: "1px solid var(--panel-border)",
                  borderLeft: isSel ? `2px solid ${c}` : "2px solid transparent",
                  background: isSel ? `${c}18` : isHov ? "var(--surface)" : "transparent",
                  cursor: "pointer",
                  transition: "background 0.15s, border-left 0.1s",
                  display: "flex", alignItems: "center", gap: 8,
                }}
              >
                {/* Rank number */}
                <div style={{
                  fontFamily:"var(--font-display)", fontSize:8,
                  color: i < 3 ? c : "var(--text-dim)", width:16, flexShrink:0,
                }}>
                  {String(i + 1).padStart(2, "0")}
                </div>

                {/* Vertical risk bar */}
                <div style={{
                  width:3, height:28, background:"var(--panel-border)",
                  borderRadius:2, flexShrink:0, overflow:"hidden", position:"relative",
                }}>
                  <div style={{
                    position:"absolute", bottom:0, left:0, right:0,
                    height:`${score}%`, background:c, borderRadius:2,
                  }}/>
                </div>

                {/* Name + meta */}
                <div style={{ flex:1, minWidth:0 }}>
                  <div style={{
                    fontFamily:"var(--font-mono)", fontSize:9,
                    color: isSel ? c : "var(--text-primary)",
                    fontWeight: isSel ? 700 : 400,
                    overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap",
                  }}>
                    {obj.OBJECT_NAME || `NORAD #${id}`}
                  </div>
                  <div style={{ display:"flex", gap:8, marginTop:2 }}>
                    <span style={{ fontSize:7, color:"var(--text-dim)" }}>
                      {parseFloat(obj.APOAPSIS||0).toFixed(0)}km
                    </span>
                    <span style={{ fontSize:7, color:"var(--text-dim)" }}>{obj.COUNTRY_CODE||"—"}</span>
                    <span style={{ fontSize:7, color:"var(--text-dim)" }}>{obj.LAUNCH_YEAR||"—"}</span>
                  </div>
                </div>

                {/* Score + badge + state indicator */}
                <div style={{ textAlign:"right", flexShrink:0, minWidth:58 }}>
                  {/* Score number in the risk tier's colour */}
                  <div style={{
                    fontFamily:"var(--font-display)", fontSize:13,
                    fontWeight:700, color:c, lineHeight:1,
                  }}>
                    {score}
                  </div>

                  {/* Coloured badge — reads level from shared scorer */}
                  <div
                    className={`risk-badge ${level}`}
                    style={{
                      marginTop:2,
                      background:`${c}22`,
                      border:`1px solid ${c}66`,
                      color:c,
                      fontSize:7, fontFamily:"var(--font-mono)", letterSpacing:"0.05em",
                      padding:"1px 5px", borderRadius:2, display:"inline-block",
                    }}
                  >
                    {level}
                  </div>

                  {/* State indicator — TRACKING / LOCATE */}
                  {isSel ? (
                    <div style={{
                      fontSize:7, color:c, marginTop:3, letterSpacing:"0.06em",
                      animation:"feedPulse 1.4s ease-in-out infinite",
                    }}>
                      ◉ TRACKING
                    </div>
                  ) : isHov ? (
                    <div style={{
                      fontSize:7, color:"rgba(6,182,212,0.6)",
                      marginTop:3, letterSpacing:"0.06em",
                    }}>
                      ⊕ LOCATE
                    </div>
                  ) : null}
                </div>
              </div>
            );
          })
        )}
      </div>

      <style>{`
        @keyframes feedPulse { 0%,100%{opacity:0.85} 50%{opacity:0.3} }
      `}</style>
    </div>
  );
}