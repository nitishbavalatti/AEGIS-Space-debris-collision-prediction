import { useState, useEffect } from "react";
import "./index.css";
import { useAegis } from "./hooks/useAegis.jsx";
import TopBar from "./components/TopBar";
import Globe from "./components/Globe";
import ISSAlert from "./components/ISSAlert";
import SolarCorrelation from "./components/SolarCorrelation";
import ThreatFeed from "./components/ThreatFeed";
import KesslerSimulator from "./components/KesslerSimulator";
import SolarWeather from "./components/SolarWeather";
import RFMonitor from "./components/RFMonitor";
import CollisionPopup from "./components/CollisionPopup";
import ConjunctionTimeline from "./components/ConjunctionTimeline";

function Cell({ children, style = {} }) {
  return (
    <div style={{
      minHeight: 0,
      maxHeight: "100%",
      height: "100%",
      overflow: "hidden",
      display: "flex",
      flexDirection: "column",
      ...style,
    }}>
      {children}
    </div>
  );
}

export default function App() {
  const aegis = useAegis();
  const [showConj, setShowConj] = useState(false);

  // Close the conjunction pop-up with Esc
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") setShowConj(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const realConj = (aegis.conjunctions ?? []).filter(c => !c._is_mock);
  // Show "scanning" until the FIRST run has completed, so "still screening" doesn't
  // read as "0 conjunctions".
  const conjScanning = !!aegis.conjunctionsScreening && !aegis.lastScreened;
  const conjAlert = realConj.some(c => c.alert === "RED") ? "RED"
                  : realConj.some(c => c.alert === "AMBER") ? "AMBER" : null;

  return (
    <div style={{
      display: "flex",
      flexDirection: "column",
      height: "100vh",
      width: "100vw",
      overflow: "hidden",
      background: "var(--void)",
    }}>
      <TopBar
        connected={aegis.connected}
        sensorSource={aegis.sensorSource}
        sensorDetail={aegis.sensorDetail}
        debrisCount={aegis.debrisCount}
        currentRisk={aegis.currentRisk}
        solarAlert={aegis.solarAlert}
        conjunctionCount={realConj.length}
        conjunctionAlert={conjAlert}
        conjunctionScanning={conjScanning}
        onOpenConjunctions={() => setShowConj(true)}
      />

      {showConj && (
        <div
          onClick={() => setShowConj(false)}
          style={{
            position: "fixed", inset: 0, zIndex: 500,
            background: "rgba(2,4,8,0.72)",
            display: "flex", alignItems: "center", justifyContent: "center",
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{ width: "min(560px, 92vw)", height: "min(72vh, 600px)", position: "relative" }}
          >
            <button
              onClick={() => setShowConj(false)}
              aria-label="Close"
              style={{
                position: "absolute", top: 8, right: 10, zIndex: 2,
                background: "none", border: "none", cursor: "pointer",
                color: "var(--text-dim)", fontSize: 14,
              }}
            >✕</button>
            <ConjunctionTimeline
              conjunctions={aegis.conjunctions}
              lastScreened={aegis.lastScreened}
              isLoading={aegis.conjunctionsLoading || conjScanning}
            />
          </div>
        </div>
      )}

      <div style={{
        flex: 1,
        minHeight: 0,
        display: "grid",
        gridTemplateColumns: "240px 1fr 260px",
        gridTemplateRows: "1fr 1fr 1fr 1.5fr",
        gap: 3,
        padding: 4,
        boxSizing: "border-box",
        overflow: "hidden",
      }}>

        {/* LEFT col rows 1-3: Threat Feed */}
        <div style={{ gridRow: "1 / 4", gridColumn: "1", minHeight: 0, overflow: "hidden" }}>
          <Cell style={{ overflow: "auto" }}>
            <ThreatFeed topThreats={aegis.topThreats} loadMoreDebris={aegis.loadMoreDebris} isMock={aegis.debrisIsMock} />
          </Cell>
        </div>

        {/* CENTER rows 1-3: Globe + CollisionPopup */}
        <div style={{
          gridRow: "1 / 4", gridColumn: "2",
          minHeight: 0, overflow: "hidden",
          position: "relative",
        }}>
          <Globe
            debrisObjects={aegis.debrisObjects}
            debrisCount={aegis.debrisCount}
            issPosition={aegis.issPosition}
            conjunctions={aegis.conjunctions}
            tierCounts={aegis.tierCounts}
            isMock={aegis.debrisIsMock}
          />
          <CollisionPopup conjunctions={aegis.conjunctions} />
        </div>

        {/* RIGHT col spans ALL 4 rows with rebalanced flex */}
        <div style={{
          gridRow: "1 / 5",
          gridColumn: "3",
          minHeight: 0,
          overflow: "hidden",
          display: "flex",
          flexDirection: "column",
          gap: 3,
        }}>
          {/* ISS Alert — gets more space (1.4) */}
          <div style={{ flex: 1, minHeight: 0, overflow: "hidden" }}>
            <Cell style={{ overflow: "auto" }}>
              <ISSAlert issPosition={aegis.issPosition} issProximity={aegis.issProximity} />
            </Cell>
          </div>

          {/* RF Monitor — gets less space (0.6), no wasted bottom gap */}
          <div style={{ flex: 1, minHeight: 0, overflow: "hidden" }}>
            <Cell style={{ overflow: "auto" }}>
              <RFMonitor latestSensor={aegis.latestSensor} />
            </Cell>
          </div>

          {/* Kessler Simulator — same as before (1) */}
          <div style={{ flex: 1, minHeight: 0, overflow: "hidden" }}>
            <Cell>
              <KesslerSimulator />
            </Cell>
          </div>
        </div>

        {/* BOTTOM row col 1: Solar Weather */}
        <div style={{ gridRow: "4", gridColumn: "1", minHeight: 0, overflow: "hidden" }}>
          <Cell>
            <SolarWeather solarSummary={aegis.solarSummary} />
          </Cell>
        </div>

        {/* BOTTOM row col 2: Solar Correlation */}
        <div style={{ gridRow: "4", gridColumn: "2", minHeight: 0, overflow: "hidden" }}>
          <Cell>
            <SolarCorrelation
              correlationSeries={aegis.correlationSeries}
              latestSensor={aegis.latestSensor}
              solarSummary={aegis.solarSummary}
            />
          </Cell>
        </div>

      </div>
    </div>
  );
}