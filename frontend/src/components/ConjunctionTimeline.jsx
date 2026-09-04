/**
 * ConjunctionTimeline — shows upcoming predicted close approaches between
 * ISS and debris objects, ranked by miss distance.
 *
 * Data source: GET /api/conjunction/upcoming (SGP4 pairwise propagation)
 * Refreshes whenever useAegis fires a new conjunctions update.
 */
import { useState, useEffect, useRef } from "react";

const ALERT_COLOR = {
  RED:    "var(--red)",
  AMBER:  "var(--amber)",
  YELLOW: "#eab308",
};

const ALERT_DOT = {
  RED:    "red",
  AMBER:  "amber",
  YELLOW: "amber",
};

function TCACountdown({ tcaHours }) {
  const [display, setDisplay] = useState("");

  useEffect(() => {
    function fmt() {
      const totalSec = Math.max(0, Math.round(tcaHours * 3600));
      if (totalSec <= 0) return "NOW";
      const h = Math.floor(totalSec / 3600);
      const m = Math.floor((totalSec % 3600) / 60);
      const s = totalSec % 60;
      return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
    }

    // Subtract elapsed time each second
    const startMs  = Date.now();
    const startSec = tcaHours * 3600;

    const tick = () => {
      const elapsed = (Date.now() - startMs) / 1000;
      const remaining = Math.max(0, startSec - elapsed);
      const h = Math.floor(remaining / 3600);
      const m = Math.floor((remaining % 3600) / 60);
      const s = Math.floor(remaining % 60);
      if (remaining <= 0) {
        setDisplay("NOW");
      } else {
        setDisplay(`${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`);
      }
    };

    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [tcaHours]);

  return <span>{display}</span>;
}

export default function ConjunctionTimeline({ conjunctions, lastScreened, isLoading }) {
  const [selectedId, setSelectedId] = useState(null);
  const listRef = useRef(null);

  // Auto-select first RED event on new data
  useEffect(() => {
    if (!conjunctions?.length) return;
    const first = conjunctions[0];
    if (first?.alert === "RED") {
      setSelectedId(first.norad_id);
    }
  }, [conjunctions]);

  const hasData = conjunctions && conjunctions.length > 0;

  const highestAlert = conjunctions?.find(c => c.alert === "RED")
    ? "RED"
    : conjunctions?.find(c => c.alert === "AMBER")
    ? "AMBER"
    : "GREEN";

  const dotClass = highestAlert === "RED"
    ? "red"
    : highestAlert === "AMBER"
    ? "amber"
    : "dim";

  return (
    <div className="panel" style={{ height: "100%", display: "flex", flexDirection: "column" }}>

      {/* Header */}
      <div className="panel-header" style={{ flexShrink: 0, gap: 8 }}>
        <div className={`status-dot ${dotClass}`} />
        <span className="panel-title">Conjunction Timeline · 72h Prediction</span>
        <span style={{
          marginLeft: "auto",
          fontFamily: "var(--font-mono)",
          fontSize: 8,
          color: "var(--text-dim)",
        }}>
          {hasData ? `${conjunctions.length} EVENTS` : isLoading ? "SCANNING..." : "NO EVENTS"}
        </span>
      </div>

      {/* Last screened timestamp */}
      {lastScreened && (
        <div style={{
          padding: "3px 10px",
          fontSize: 7,
          color: "var(--text-dim)",
          borderBottom: "var(--border)",
          fontFamily: "var(--font-mono)",
          letterSpacing: "0.05em",
        }}>
          LAST SCREENED: {new Date(lastScreened).toLocaleTimeString()} · SGP4 ×{" "}
          {conjunctions?.length ?? 0} EVENTS IN 72H
        </div>
      )}

      {/* List */}
      <div ref={listRef} style={{ flex: 1, overflowY: "auto", padding: "2px 0" }}>

        {isLoading && !hasData && (
          <div style={{
            padding: 20, textAlign: "center",
            color: "var(--text-dim)", fontSize: 9, letterSpacing: "0.12em",
          }}>
            <div style={{ marginBottom: 6, opacity: 0.5 }}>◌</div>
            RUNNING SGP4 PROPAGATION...
          </div>
        )}

        {!isLoading && !hasData && (
          <div style={{
            padding: 20, textAlign: "center",
            color: "var(--text-dim)", fontSize: 9, letterSpacing: "0.12em",
          }}>
            <div style={{ marginBottom: 6, color: "var(--green)", opacity: 0.6 }}>✓</div>
            NO CONJUNCTIONS WITHIN 25km / 72h
          </div>
        )}

        {(conjunctions ?? []).map((event, i) => {
          const color    = ALERT_COLOR[event.alert] ?? "var(--text-dim)";
          const isSel    = selectedId === event.norad_id;
          const isMock   = event._is_mock;

          return (
            <div
              key={event.norad_id + i}
              onClick={() => setSelectedId(isSel ? null : event.norad_id)}
              style={{
                padding: "7px 10px",
                borderBottom: "1px solid var(--panel-border)",
                borderLeft: isSel ? `2px solid ${color}` : "2px solid transparent",
                background: isSel ? `${color}12` : "transparent",
                cursor: "pointer",
                transition: "background 0.15s",
              }}
            >
              {/* Row 1: name + alert badge + TCA countdown */}
              <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 3 }}>
                <div style={{
                  fontFamily: "var(--font-mono)", fontSize: 9,
                  color: isSel ? color : "var(--text-primary)",
                  flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                }}>
                  {event.object_name}
                  {isMock && (
                    <span style={{ fontSize: 7, color: "var(--text-dim)", marginLeft: 4 }}>
                      [MOCK]
                    </span>
                  )}
                </div>

                {/* Alert badge */}
                <div style={{
                  padding: "1px 6px",
                  background: `${color}20`,
                  border: `1px solid ${color}60`,
                  borderRadius: 2,
                  fontFamily: "var(--font-display)",
                  fontSize: 7,
                  color,
                  flexShrink: 0,
                }}>
                  {event.alert}
                </div>
              </div>

              {/* Row 2: miss distance + TCA countdown + Pc */}
              <div style={{ display: "flex", gap: 10, alignItems: "center" }}>

                {/* Miss distance */}
                <div style={{ textAlign: "center", minWidth: 52 }}>
                  <div style={{
                    fontFamily: "var(--font-display)",
                    fontSize: 14, fontWeight: 700,
                    color, lineHeight: 1,
                  }}>
                    {event.miss_distance_km.toFixed(2)}
                  </div>
                  <div style={{ fontSize: 7, color: "var(--text-dim)", marginTop: 1 }}>km</div>
                </div>

                {/* Divider */}
                <div style={{ width: 1, height: 24, background: "var(--panel-border)", flexShrink: 0 }} />

                {/* TCA countdown */}
                <div style={{ textAlign: "center", flex: 1 }}>
                  <div style={{
                    fontFamily: "var(--font-display)",
                    fontSize: 11, fontWeight: 700,
                    color: "var(--cyan)", letterSpacing: "0.05em",
                  }}>
                    <TCACountdown tcaHours={event.tca_hours} />
                  </div>
                  <div style={{ fontSize: 7, color: "var(--text-dim)", marginTop: 1 }}>
                    TCA COUNTDOWN
                  </div>
                </div>

                {/* Divider */}
                <div style={{ width: 1, height: 24, background: "var(--panel-border)", flexShrink: 0 }} />

                {/* Probability */}
                <div style={{ textAlign: "right", minWidth: 44 }}>
                  <div style={{
                    fontFamily: "var(--font-display)",
                    fontSize: 10, fontWeight: 700,
                    color: event.pc_percent > 0.001 ? "var(--red)" : "var(--text-secondary)",
                  }}>
                    {event.pc_percent < 0.0001
                      ? "<0.0001"
                      : event.pc_percent.toFixed(4)}%
                  </div>
                  <div style={{ fontSize: 7, color: "var(--text-dim)", marginTop: 1 }}>Pc</div>
                </div>
              </div>

              {/* Expanded detail row */}
              {isSel && (
                <div style={{
                  marginTop: 6, paddingTop: 6,
                  borderTop: `1px solid ${color}30`,
                  display: "grid", gridTemplateColumns: "1fr 1fr 1fr",
                  gap: 4,
                }}>
                  {[
                    ["REL VEL",    `${event.relative_vel_km_s} km/s`],
                    ["ALTITUDE",   `${parseFloat(event.apoapsis || 0).toFixed(0)} km`],
                    ["INCLINATION",`${parseFloat(event.inclination || 0).toFixed(1)}°`],
                    ["COUNTRY",    event.country_code || "—"],
                    ["NORAD",      `#${event.norad_id}`],
                    ["GNN RISK",   `${event.risk_score ?? "—"}`],
                  ].map(([label, value]) => (
                    <div key={label}>
                      <div style={{ fontSize: 6, color: "var(--text-dim)", letterSpacing: "0.1em" }}>
                        {label}
                      </div>
                      <div style={{ fontFamily: "var(--font-mono)", fontSize: 9, color: "var(--text-secondary)" }}>
                        {value}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Footer — methodology note */}
      <div style={{
        padding: "4px 10px",
        borderTop: "var(--border)",
        fontSize: 7,
        color: "var(--text-dim)",
        fontFamily: "var(--font-mono)",
        letterSpacing: "0.04em",
        flexShrink: 0,
      }}>
        SGP4 · 60s SAMPLE · FOSTER Pc · σ=0.2/0.5/0.2km
      </div>
    </div>
  );
}