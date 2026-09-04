export default function SolarWeather({ solarSummary }) {
  if (!solarSummary) {
    return (
      <div className="panel" style={{ height: "100%", display: "flex", flexDirection: "column" }}>
        <div className="panel-header" style={{ flexShrink: 0 }}>
          <div className="status-dot dim" />
          <span className="panel-title">Solar Weather · NOAA SWPC</span>
        </div>
        <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-dim)", fontSize: 9 }}>
          LOADING...
        </div>
      </div>
    );
  }

  const levelColor = {
    NONE: "var(--green)", MINOR: "var(--cyan)",
    MODERATE: "var(--amber)", STRONG: "#fb923c", SEVERE: "var(--red)",
  }[solarSummary.storm_level] || "var(--green)";

  const stats = [
    { label: "FLARES",   value: solarSummary.flare_count   ?? 0, color: "var(--amber)" },
    { label: "X-CLASS", value: solarSummary.x_class_flares || "—", color: "var(--red)" },
    { label: "M-CLASS", value: solarSummary.m_class_flares || "—", color: "var(--amber)" },
    { label: "CME",     value: solarSummary.cme_count      || "—", color: "var(--cyan)"  },
  ];

  return (
    <div className="panel" style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <div className="panel-header" style={{ flexShrink: 0 }}>
        <div className={`status-dot ${solarSummary.storm_level !== "NONE" ? "amber" : "dim"}`} />
        <span className="panel-title">Solar Weather · NOAA SWPC</span>
      </div>

      {solarSummary.note && (
        <div style={{ padding: "5px 8px", fontSize: 8, color: "var(--text-dim)", borderBottom: "1px solid var(--panel-border)", background: "rgba(255,255,255,0.02)" }}>
          {solarSummary.note}
        </div>
      )}

      <div style={{ flex: 1, minHeight: 0, padding: "6px 8px", display: "flex", flexDirection: "column", gap: 5, overflowY: "auto" }}>
        {/* Storm badge */}
        <div style={{
          textAlign: "center", flexShrink: 0,
          background: `${levelColor}15`, border: `1px solid ${levelColor}50`,
          borderRadius: 2, padding: "5px 4px",
        }}>
          <div style={{ fontSize: 7, letterSpacing: "0.18em", color: "var(--text-label)", marginBottom: 2 }}>GEOMAGNETIC STORM</div>
          <div style={{ fontFamily: "var(--font-display)", fontSize: 13, fontWeight: 900, color: levelColor, letterSpacing: "0.08em" }}>
            {solarSummary.storm_level}
          </div>
          <div style={{ fontFamily: "var(--font-mono)", fontSize: 8, color: levelColor, opacity: 0.7, marginTop: 1 }}>
            Kp MAX: {(solarSummary.max_kp_index ?? 0).toFixed(1)}
          </div>
        </div>

        {/* 2x2 Stats */}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4, flexShrink: 0 }}>
          {stats.map(({ label, value, color }) => (
            <div key={label} style={{ background: "var(--surface)", border: "var(--border)", borderRadius: 2, padding: "5px 6px" }}>
              <div style={{ fontFamily: "var(--font-display)", fontSize: 18, fontWeight: 700, color, lineHeight: 1 }}>
                {String(value)}
              </div>
              <div style={{ fontFamily: "var(--font-mono)", fontSize: 7, color: "var(--text-label)", marginTop: 2, letterSpacing: "0.05em" }}>
                {label} / 7d
              </div>
            </div>
          ))}
        </div>

        {/* Latest flare */}
        {solarSummary.latest_flares?.length > 0 && (
          <div style={{ flexShrink: 0, borderTop: "var(--border)", paddingTop: 5 }}>
            <div style={{ fontSize: 7, letterSpacing: "0.12em", color: "var(--text-dim)", marginBottom: 3 }}>LATEST FLARE</div>
            {solarSummary.latest_flares.slice(0, 2).map((f, i) => (
              <div key={i} style={{ display: "flex", justifyContent: "space-between", padding: "2px 0", borderBottom: "1px solid var(--panel-border)" }}>
                <span style={{ fontFamily: "var(--font-display)", fontSize: 11, color: "var(--amber)" }}>{f.classType || "—"}</span>
                <span style={{ fontFamily: "var(--font-mono)", fontSize: 8, color: "var(--text-secondary)" }}>
                  {f.peakTime ? f.peakTime.slice(0, 10) : "—"}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
