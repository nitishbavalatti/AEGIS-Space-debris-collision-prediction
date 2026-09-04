/**
 * CollisionPopup — floats over the Globe when a RED or AMBER
 * conjunction event is predicted.
 *
 * Handles two event types:
 *   debris_iss     — debris approaching ISS
 *   debris_debris  — two debris objects approaching each other
 *                    (more dangerous — potential Kessler trigger)
 *
 * Auto-cycles through all critical events every 8s.
 * Dismissible per-event. Never shows mock data.
 */
import { useState, useEffect, useRef } from "react";

export default function CollisionPopup({ conjunctions }) {
  const [visible,   setVisible]   = useState(false);
  const [current,   setCurrent]   = useState(null);
  const [dismissed, setDismissed] = useState(new Set());
  const cycleRef = useRef(null);

  // Only show real RED/AMBER events — never mock
  const critical = (conjunctions ?? []).filter(
    c => (c.alert === "RED" || c.alert === "AMBER") && !c._is_mock
  );

  // Show top undismissed event whenever critical list changes
  useEffect(() => {
    const next = critical.find(c => !dismissed.has(c.norad_id));
    if (next) {
      setCurrent(next);
      setVisible(true);
    } else {
      setVisible(false);
    }
  }, [conjunctions]); // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-cycle through events every 8s when there are multiple
  useEffect(() => {
    if (cycleRef.current) clearInterval(cycleRef.current);
    if (critical.length <= 1) return;

    cycleRef.current = setInterval(() => {
      setCurrent(prev => {
        const idx  = critical.findIndex(c => c.norad_id === prev?.norad_id);
        return critical[(idx + 1) % critical.length];
      });
    }, 8000);

    return () => clearInterval(cycleRef.current);
  }, [critical.length]); // eslint-disable-line react-hooks/exhaustive-deps

  const dismiss = () => {
    if (!current) return;
    setDismissed(prev => new Set([...prev, current.norad_id]));
    const next = critical.find(
      c => c.norad_id !== current.norad_id && !dismissed.has(c.norad_id)
    );
    if (next) {
      setCurrent(next);
    } else {
      setVisible(false);
    }
  };

  if (!visible || !current) return null;

  const isRed   = current.alert === "RED";
  const isDxD   = current._type === "debris_debris";
  const isKessler = isDxD && current.kessler_risk;

  const color = isRed ? "#ef4444" : "#f59e0b";
  const glow  = isRed ? "0 0 28px #ef444466" : "0 0 28px #f59e0b66";

  // TCA live countdown
  const [countdown, setCountdown] = useState("");
  useEffect(() => {
    if (!current?.tca_hours) return;
    const startMs  = Date.now();
    const startSec = current.tca_hours * 3600;

    const tick = () => {
      const remaining = Math.max(0, startSec - (Date.now() - startMs) / 1000);
      if (remaining === 0) { setCountdown("NOW"); return; }
      const h = Math.floor(remaining / 3600);
      const m = Math.floor((remaining % 3600) / 60);
      const s = Math.floor(remaining % 60);
      setCountdown(
        `${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`
      );
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [current?.norad_id]); // reset timer when event changes

  return (
    <div style={{
      position:    "absolute",
      bottom:      16,
      left:        "50%",
      transform:   "translateX(-50%)",
      zIndex:      100,
      width:       360,
      background:  "#060d14",
      border:      `1px solid ${color}`,
      borderRadius: 4,
      boxShadow:   glow,
      fontFamily:  "var(--font-mono)",
      pointerEvents: "all",
      animation:   "popupSlide 0.3s ease",
    }}>
      <style>{`
        @keyframes popupSlide {
          from { opacity: 0; transform: translateX(-50%) translateY(12px); }
          to   { opacity: 1; transform: translateX(-50%) translateY(0); }
        }
        @keyframes dotPulse {
          0%,100% { opacity:1; box-shadow: 0 0 6px ${color}; }
          50%      { opacity:0.4; box-shadow: 0 0 14px ${color}; }
        }
      `}</style>

      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <div style={{
        display: "flex", alignItems: "center", gap: 8,
        padding: "7px 10px",
        borderBottom: `1px solid ${color}30`,
        background: `${color}12`,
      }}>
        <div style={{
          width: 8, height: 8, borderRadius: "50%",
          background: color,
          animation: "dotPulse 1s ease-in-out infinite",
          flexShrink: 0,
        }} />

        <span style={{ fontSize: 9, fontWeight: 700, letterSpacing: "0.12em", color, flex: 1 }}>
          {isRed ? "⚠ CRITICAL COLLISION RISK" : "⚠ CONJUNCTION WARNING"}
        </span>

        {/* Event type badge */}
        <span style={{
          fontSize: 7, padding: "1px 6px",
          background: isDxD
            ? "rgba(239,68,68,0.2)"
            : "rgba(6,182,212,0.15)",
          border: `1px solid ${isDxD ? "#ef4444" : "#06b6d4"}60`,
          borderRadius: 2,
          color: isDxD ? "#ef4444" : "#06b6d4",
          fontFamily: "var(--font-display)",
          letterSpacing: "0.08em",
        }}>
          {isDxD
            ? isKessler ? "⚡ KESSLER TRIGGER" : "DEBRIS × DEBRIS"
            : "DEBRIS × ISS"}
        </span>

        <button onClick={dismiss} style={{
          background: "none", border: "none", cursor: "pointer",
          color: "var(--text-dim)", fontSize: 12, padding: "0 2px", lineHeight: 1,
        }}>✕</button>
      </div>

      {/* ── Object name(s) ─────────────────────────────────────────────────── */}
      <div style={{ padding: "8px 12px 4px", borderBottom: `1px solid ${color}15` }}>
        {isDxD ? (
          // Two objects for D×D
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <div style={{
              flex: 1, fontSize: 9, color: "var(--text-primary)",
              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
            }}>
              {current.object_name}
            </div>
            <div style={{ fontSize: 8, color, flexShrink: 0 }}>↔</div>
            <div style={{
              flex: 1, fontSize: 9, color: "var(--text-primary)",
              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
              textAlign: "right",
            }}>
              {current.object_name_b || `NORAD #${current.norad_id_b}`}
            </div>
          </div>
        ) : (
          // Single object for D×ISS
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <div style={{
              flex: 1, fontSize: 9, color: "var(--text-primary)",
              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
            }}>
              {current.object_name}
            </div>
            <div style={{ fontSize: 8, color, flexShrink: 0 }}>↔ ISS</div>
          </div>
        )}

        {/* Kessler warning */}
        {isKessler && (
          <div style={{
            marginTop: 4, fontSize: 7,
            color: "#ef4444", letterSpacing: "0.08em",
          }}>
            ⚡ HYPERVELOCITY IMPACT — KESSLER CASCADE RISK
          </div>
        )}
      </div>

      {/* ── Stats grid ─────────────────────────────────────────────────────── */}
      <div style={{
        display: "grid", gridTemplateColumns: "1fr 1fr 1fr",
        gap: 8, padding: "10px 12px 8px",
      }}>
        {[
          ["MISS DIST",
           `${current.miss_distance_km} km`,
           color],
          ["TCA IN",
           countdown || `${current.tca_hours?.toFixed(1)}h`,
           "var(--cyan)"],
          ["Pc",
           current.pc_percent < 0.0001
             ? "<0.0001%"
             : `${current.pc_percent?.toFixed(4)}%`,
           current.pc_percent > 0.001 ? color : "var(--text-secondary)"],
        ].map(([label, val, col]) => (
          <div key={label} style={{ textAlign: "center" }}>
            <div style={{ fontSize: 6, color: "var(--text-dim)", letterSpacing: "0.1em", marginBottom: 2 }}>
              {label}
            </div>
            <div style={{ fontSize: 13, fontWeight: 700, color: col, lineHeight: 1, fontFamily: "var(--font-display)" }}>
              {val}
            </div>
          </div>
        ))}
      </div>

      {/* ── Detail row ─────────────────────────────────────────────────────── */}
      <div style={{
        display: "flex", justifyContent: "space-between",
        padding: "0 12px 8px",
        fontSize: 8, color: "var(--text-dim)",
        borderBottom: `1px solid ${color}15`,
      }}>
        <span>
          VEL: <span style={{ color: "var(--text-secondary)" }}>
            {current.relative_vel_km_s} km/s
          </span>
        </span>
        <span>
          ALT: <span style={{ color: "var(--text-secondary)" }}>
            {parseFloat(current.apoapsis || 0).toFixed(0)} km
          </span>
        </span>
        <span>
          {isDxD ? (
            <>
              {current.country_code}
              <span style={{ color: "var(--text-dim)" }}> × </span>
              {current.country_code_b}
            </>
          ) : (
            <>NORAD: <span style={{ color: "var(--text-secondary)" }}>{current.norad_id}</span></>
          )}
        </span>
      </div>

      {/* ── Footer ─────────────────────────────────────────────────────────── */}
      <div style={{
        padding: "4px 12px",
        fontSize: 7, color: "var(--text-dim)",
        display: "flex", justifyContent: "space-between", alignItems: "center",
      }}>
        <span style={{ color: "var(--text-dim)" }}>
          SGP4 · FOSTER Pc · REAL DATA
        </span>
        {critical.length > 1 && (
          <span style={{ color }}>
            {critical.length} EVENTS · AUTO-CYCLING ›
          </span>
        )}
      </div>
    </div>
  );
}