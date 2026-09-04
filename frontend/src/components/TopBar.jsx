import { useState, useEffect } from "react";

export default function TopBar({ connected, sensorSource, debrisCount, currentRisk, solarAlert }) {
  const [time, setTime] = useState(new Date());

  useEffect(() => {
    const t = setInterval(() => setTime(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  const istParts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata",
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(time);
  const istMap = Object.fromEntries(istParts.map((part) => [part.type, part.value]));
  const ist = `${istMap.year}-${istMap.month}-${istMap.day} ${istMap.hour}:${istMap.minute}:${istMap.second} IST`;
  const missionTime = Math.floor((Date.now() - new Date("2025-01-01").getTime()) / 1000);
  const missionStr = new Date(missionTime * 1000).toISOString().slice(11, 19);

  return (
    <header style={{
      display: "flex",
      alignItems: "center",
      justifyContent: "space-between",
      padding: "0 24px",
      height: "52px",
      background: "var(--deep)",
      borderBottom: "1px solid var(--panel-border)",
      flexShrink: 0,
      position: "relative",
      overflow: "hidden",
    }}>
      {/* Left glow line */}
      <div style={{
        position: "absolute", top: 0, left: 0, right: 0, height: "1px",
        background: "linear-gradient(90deg, var(--cyan), var(--amber), transparent)",
      }} />

      {/* Logo */}
      <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
        <div style={{ position: "relative" }}>
          <div style={{
            width: 32, height: 32,
            border: "2px solid var(--cyan)",
            borderRadius: "50%",
            display: "flex", alignItems: "center", justifyContent: "center",
            background: "var(--cyan-glow)",
          }}>
            <div style={{
              width: 6, height: 6,
              borderRadius: "50%",
              background: "var(--amber)",
              boxShadow: "0 0 8px var(--amber)",
            }} />
          </div>
          {/* Orbit ring */}
          <div style={{
            position: "absolute", inset: -4,
            border: "1px solid var(--cyan-dim)",
            borderRadius: "50%",
            borderTopColor: "transparent",
            animation: "spin 4s linear infinite",
          }} />
        </div>

        <div>
          <div style={{
            fontFamily: "var(--font-display)",
            fontSize: 18,
            fontWeight: 900,
            letterSpacing: "0.3em",
            color: "var(--text-primary)",
            lineHeight: 1,
          }}>
            AEGIS
          </div>
          <div style={{
            fontSize: 8,
            letterSpacing: "0.25em",
            color: "var(--text-dim)",
            textTransform: "uppercase",
          }}>
            Space Debris Threat Monitor · Bengaluru GS
          </div>
        </div>
      </div>

      {/* Center — solar storm banner */}
      {solarAlert && (
        <div style={{
          position: "absolute", left: "50%", transform: "translateX(-50%)",
          background: "var(--red-dim)",
          border: "1px solid var(--red)",
          padding: "4px 16px",
          borderRadius: 2,
          fontFamily: "var(--font-display)",
          fontSize: 9,
          letterSpacing: "0.2em",
          color: "var(--red)",
          display: "flex", alignItems: "center", gap: 8,
        }}>
          <span className="blink">▲</span>
          SOLAR STORM DETECTED · KP {solarAlert.kp?.toFixed(1) ?? solarAlert.kp_proxy?.toFixed(1) ?? "-"} · {solarAlert.level || solarAlert.alert_level || "UNKNOWN"}
        </div>
      )}

      <div style={{ display: "flex", alignItems: "center", gap: 24 }}>
        {/* Sensor mode */}
        {(() => {
          const hardwareConnected = sensorSource === "esp32" || sensorSource === "hardware";
          return (
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <div className={`status-dot ${hardwareConnected ? "green" : "amber"}`} />
              <div>
                <div style={{ fontSize: 8, letterSpacing: "0.15em", color: "var(--text-label)", textTransform: "uppercase" }}>
                  Sensor
                </div>
                <div style={{ fontFamily: "var(--font-display)", fontSize: 9, color: hardwareConnected ? "var(--green)" : "var(--amber)" }}>
                  {hardwareConnected ? "HARDWARE" : "SIMULATED"}
                </div>
              </div>
            </div>
          );
        })()}

        {/* Connection */}
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <div className={`status-dot ${connected ? "green" : "red"}`} />
          <span style={{ fontSize: 9, color: connected ? "var(--green)" : "var(--red)", fontFamily: "var(--font-display)" }}>
            {connected ? "LIVE" : "OFFLINE"}
          </span>
        </div>

        {/* Clock */}
        <div style={{ textAlign: "right" }}>
          <div style={{ fontFamily: "var(--font-display)", fontSize: 11, color: "var(--amber)", letterSpacing: "0.1em" }}>
            {ist}
          </div>
          <div style={{ fontSize: 8, color: "var(--text-dim)", letterSpacing: "0.15em" }}>
            Bengaluru IST · MET {missionStr}
          </div>
        </div>
      </div>

      <style>{`
        @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
      `}</style>
    </header>
  );
}

function Stat({ label, value, color }) {
  return (
    <div>
      <div style={{ fontSize: 8, letterSpacing: "0.15em", color: "var(--text-label)", textTransform: "uppercase" }}>
        {label}
      </div>
      <div style={{ fontFamily: "var(--font-display)", fontSize: 13, fontWeight: 700, color, letterSpacing: "0.05em" }}>
        {value}
      </div>
    </div>
  );
}
