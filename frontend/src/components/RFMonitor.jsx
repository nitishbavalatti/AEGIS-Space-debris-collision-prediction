/**
 * RF Monitor — displays RTL-SDR noise floor readings
 * alert_level, storm_detected come DIRECTLY from backend — never recalculated.
 * Only noise_dbm and delta_dbfs get a tiny visual jitter (±0.3) per second
 * to show the feed is live when the parent prop cadence is slow.
 */
import { useEffect, useRef, useState } from "react";

function normalizeNumeric(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export default function RFMonitor({ latestSensor }) {
  const sensorRef = useRef(latestSensor);
  const [display, setDisplay] = useState(null);

  useEffect(() => {
    sensorRef.current = latestSensor;
  }, [latestSensor]);

  useEffect(() => {
    const tick = () => {
      const s = sensorRef.current;
      if (!s?.rf) return;

      const rf = s.rf;

      setDisplay({
        // ── from backend, never touched ──────────────────────────────────
        alert_level:    rf.alert_level    ?? "NONE",
        storm_detected: rf.storm_detected ?? false,
        baseline_dbfs:  normalizeNumeric(rf.baseline_dbfs, -39.7),

        // ── show the raw sensor readings directly for consistency
        noise_dbm:  normalizeNumeric(rf.noise_dbm, -92),
        delta_dbfs: normalizeNumeric(rf.delta_dbfs, 0),

        per_frequency: s.per_frequency
          ? Object.fromEntries(
              Object.entries(s.per_frequency).map(([f, v]) => [f, normalizeNumeric(v, 0)])
            )
          : null,

        timestamp: new Date().toISOString(),
      });
    };

    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);

  if (!display) {
    return (
      <div className="panel" style={{ height: "100%", display: "flex", flexDirection: "column" }}>
        <div className="panel-header" style={{ flexShrink: 0 }}>
          <div className="status-dot dim" />
          <span className="panel-title">RF Monitor · RTL-SDR@137MHz</span>
        </div>
        <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-dim)", fontSize: 9 }}>
          WAITING FOR DATA...
        </div>
      </div>
    );
  }

  const alertColor = {
    NONE:     "var(--green)",
    MINOR:    "var(--cyan)",
    MODERATE: "var(--amber)",
    STRONG:   "#fb923c",
    SEVERE:   "var(--red)",
  }[display.alert_level] ?? "var(--green)";

  const alertDot = {
    NONE:     "dim",
    MINOR:    "cyan",
    MODERATE: "amber",
    STRONG:   "amber",
    SEVERE:   "red",
  }[display.alert_level] ?? "dim";

  return (
    <div className="panel" style={{ height: "100%", display: "flex", flexDirection: "column", overflowY: "auto" }}>
      <div className="panel-header" style={{ flexShrink: 0 }}>
        <div className={`status-dot ${alertDot}`} />
        <span className="panel-title">RF Monitor · RTL-SDR@137MHz</span>
      </div>

      <div style={{ flex: 1, minHeight: 0, padding: "6px 8px", display: "flex", flexDirection: "column", gap: 5, overflow: "hidden" }}>

        {/* Alert Badge */}
        <div style={{
          textAlign: "center", flexShrink: 0,
          background: `${alertColor}15`,
          border: `1px solid ${alertColor}50`,
          borderRadius: 2, padding: "5px 4px",
        }}>
          <div style={{ fontSize: 7, letterSpacing: "0.18em", color: "var(--text-label)", marginBottom: 2 }}>RF ANOMALY</div>
          <div style={{ fontFamily: "var(--font-display)", fontSize: 13, fontWeight: 900, color: alertColor, letterSpacing: "0.08em" }}>
            {display.alert_level}
          </div>
          <div style={{ fontFamily: "var(--font-mono)", fontSize: 8, color: alertColor, opacity: 0.7, marginTop: 1 }}>
            Δ {display.delta_dbfs.toFixed(2)} dBFS
          </div>
        </div>

        {/* 2×2 Stats Grid */}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4, flexShrink: 0 }}>
          <Stat
            value={display.noise_dbm.toFixed(2)}
            label="NOISE dBm"
            color="var(--cyan)"
            size={14}
          />
          <Stat
            value={(display.delta_dbfs > 0 ? "+" : "") + display.delta_dbfs.toFixed(2)}
            label="DELTA dBFS"
            color={Math.abs(display.delta_dbfs) > 4 ? "var(--red)" : "var(--green)"}
            size={14}
          />
          <Stat
            value={display.baseline_dbfs.toFixed(2)}
            label="BASELINE dBFS"
            color="var(--text-secondary)"
            size={12}
          />
          <Stat
            value={display.storm_detected ? "YES" : "NO"}
            label="STORM ACTIVE"
            color={display.storm_detected ? "var(--red)" : "var(--green)"}
            size={12}
          />
        </div>

        {/* Per-frequency breakdown */}
        {display.per_frequency && (
          <div style={{ flexShrink: 0, borderTop: "var(--border)", paddingTop: 5 }}>
            <div style={{ fontSize: 7, letterSpacing: "0.12em", color: "var(--text-dim)", marginBottom: 3 }}>NOAA FREQUENCIES</div>
            <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              {Object.entries(display.per_frequency).map(([freq, dbm]) => (
                <div key={freq} style={{ display: "flex", justifyContent: "space-between", padding: "2px 0" }}>
                  <span style={{ fontFamily: "var(--font-display)", fontSize: 10, color: "var(--text-secondary)" }}>{freq}</span>
                  <span style={{ fontFamily: "var(--font-mono)", fontSize: 9, color: "var(--cyan)" }}>{Number(dbm).toFixed(2)} dBm</span>
                </div>
              ))}
            </div>
          </div>
        )}

        <div style={{ fontSize: 7, color: "var(--text-dim)", marginTop: "auto", paddingTop: 5, textAlign: "center", fontFamily: "var(--font-mono)" }}>
          {new Date(display.timestamp).toLocaleTimeString()}
        </div>
      </div>
    </div>
  );
}

function Stat({ value, label, color, size }) {
  return (
    <div style={{ background: "var(--surface)", border: "var(--border)", borderRadius: 2, padding: "5px 6px" }}>
      <div style={{ fontFamily: "var(--font-display)", fontSize: size, fontWeight: size >= 14 ? 700 : 600, color, lineHeight: 1 }}>
        {value}
      </div>
      <div style={{ fontFamily: "var(--font-mono)", fontSize: 7, color: "var(--text-label)", marginTop: 2, letterSpacing: "0.05em" }}>
        {label}
      </div>
    </div>
  );
}