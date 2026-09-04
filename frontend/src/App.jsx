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
        debrisCount={aegis.debrisCount}
        currentRisk={aegis.currentRisk}
        solarAlert={aegis.solarAlert}
      />

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
            <ThreatFeed topThreats={aegis.topThreats} loadMoreDebris={aegis.loadMoreDebris} />
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