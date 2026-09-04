import { useState } from "react";

export default function ISSAlert({ issPosition, issProximity }) {
  const dist   = issProximity?.nearest_km;
  const alert  = issProximity?.alert || "GREEN";
  const nearest = issProximity?.nearest_object;

  const alertColor = alert==="RED" ? "var(--red)" : alert==="AMBER" ? "var(--amber)" : "var(--green)";
  const dotClass   = alert==="RED" ? "red" : alert==="AMBER" ? "amber" : "green";

  return (
    <div className="panel" style={{ height:"100%", display:"flex", flexDirection:"column", overflow:"auto" }}>
      <div className="panel-header" style={{ flexShrink:0 }}>
        <div className={`status-dot ${dotClass}`} />
        <span className="panel-title">ISS Proximity Alert</span>
      </div>

      <div style={{ flex:1, minHeight:0, minWidth:0, padding:"8px 10px", display:"flex", flexDirection:"column", gap:6, overflow:"auto" }}>

        {/* Distance readout */}
        <div style={{
          flexShrink:0, textAlign:"center",
          background:`${alertColor}10`, border:`1px solid ${alertColor}40`,
          borderRadius:2, padding:"8px 8px",
        }}>
          <div style={{ fontSize:7, letterSpacing:"0.2em", color:"var(--text-label)", marginBottom:3 }}>
            NEAREST DEBRIS TO ISS
          </div>
          <div style={{
            fontFamily:"var(--font-display)", fontWeight:900,
            fontSize: dist != null ? 20 : 14,
            color: alertColor, letterSpacing:"0.02em", lineHeight:1,
          }}>
            {dist != null ? dist.toFixed(2) : "—"}
          </div>
          <div style={{ fontSize:8, color:alertColor, opacity:0.7, marginTop:2 }}>KILOMETERS</div>
          <div style={{
            marginTop:5, display:"inline-block",
            padding:"2px 10px",
            background:`${alertColor}20`, border:`1px solid ${alertColor}`,
            borderRadius:2, fontFamily:"var(--font-display)", fontSize:7,
            letterSpacing:"0.18em", color:alertColor,
          }}>
            {alert==="RED" ? "⚠ DANGER CLOSE" : alert==="AMBER" ? "CAUTION" : "NOMINAL"}
          </div>
        </div>

        {/* ISS position grid */}
        {issPosition && (
          <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:5, flexShrink:0 }}>
            {[
              ["LAT", issPosition.lat!=null ? `${issPosition.lat.toFixed(2)}°`:"—"],
              ["LON", issPosition.lon!=null ? `${issPosition.lon.toFixed(2)}°`:"—"],
              ["ALT", issPosition.alt_km!=null ? `${issPosition.alt_km.toFixed(1)} km`:"—"],
              ["SPD", "7.66 km/s"],
            ].map(([l,v])=>(
              <div key={l} style={{ background:"var(--surface)", border:"var(--border)", borderRadius:2, padding:"5px 7px" }}>
                <div style={{ fontSize:7, letterSpacing:"0.12em", color:"var(--text-label)" }}>{l}</div>
                <div style={{ fontFamily:"var(--font-mono)", fontSize:11, color:"var(--cyan)", marginTop:1 }}>{v}</div>
              </div>
            ))}
          </div>
        )}

        {/* Nearest threat */}
        {nearest && (
          <div style={{ flexShrink:0, borderTop:"var(--border)", paddingTop:5, paddingBottom:5, minWidth:0 }}>
            <div style={{ fontSize:7, letterSpacing:"0.12em", color:"var(--text-dim)", marginBottom:3 }}>
              NEAREST THREAT
            </div>
            <div style={{ fontFamily:"var(--font-mono)", fontSize:10, color:"var(--text-secondary)", whiteSpace:"normal", wordBreak:"break-word", minWidth:0 }}>
              {nearest.name || nearest.norad_id || "Unknown"}
            </div>
          </div>
        )}

        {/* Thresholds */}
        <div style={{ flexShrink:0, borderTop:"var(--border)", paddingTop:5 }}>
          <div style={{ fontSize:7, letterSpacing:"0.12em", color:"var(--text-dim)", marginBottom:4 }}>
            ALERT THRESHOLDS
          </div>
          {[
            { km:5,  label:"DANGER",  color:"var(--red)" },
            { km:10, label:"CAUTION", color:"var(--amber)" },
          ].map(({km,label,color})=>(
            <div key={km} style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:3 }}>
              <div style={{ display:"flex", alignItems:"center", gap:5 }}>
                <div style={{ width:4, height:4, borderRadius:"50%", background:color }} />
                <span style={{ fontSize:8, color }}>{label}</span>
              </div>
              <span style={{ fontFamily:"var(--font-mono)", fontSize:8, color:"var(--text-secondary)" }}>
                &lt; {km} km
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
