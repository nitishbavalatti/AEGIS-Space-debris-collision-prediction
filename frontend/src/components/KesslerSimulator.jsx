import { useState, useRef, useEffect, useCallback } from "react";

const EARTH_R = 95;
const EARTH_GLOW_R = 70;

export default function KesslerSimulator() {
  const canvasRef = useRef(null);
  const animRef = useRef(null);
  const stateRef = useRef({ particles: [], frame: 0, running: false, cascadeTriggered: false, cascadeWave: 0 });
  const [simState, setSimState] = useState("idle");
  const [stats, setStats] = useState({ collisions: 0, fragments: 0, destroyed: 0, objects: 60 });

  const W = 1200;
  const H = 700;
  const CX = W / 2;
  const CY = H / 2;

  function initParticles(count = 60) {
    const particles = [];
    for (let i = 0; i < count; i += 1) {
      const angle = Math.random() * Math.PI * 2;
      const r = EARTH_R + 20 + Math.random() * 65;
      const speed = 0.008 + Math.random() * 0.004;
      const dir = Math.random() > 0.15 ? 1 : -1;

      particles.push({
        x: CX + Math.cos(angle) * r,
        y: CY + Math.sin(angle) * r,
        angle,
        r,
        orbitSpeed: speed * dir,
        size: 3 + Math.random() * 2.5,
        color: "#22d3ee",
        isFragment: false,
        exploding: false,
        ttl: Infinity,
        mass: 0.5 + Math.random() * 2,
        id: i,
      });
    }
    return particles;
  }

  function drawScene(ctx, particles, frame, cascadeActive) {
    ctx.fillStyle = "rgba(2,4,8,0.3)";
    ctx.fillRect(0, 0, W, H);

    [EARTH_R + 20, EARTH_R + 50, EARTH_R + 85].forEach((radius) => {
      ctx.beginPath();
      ctx.arc(CX, CY, radius, 0, Math.PI * 2);
      ctx.strokeStyle = "rgba(6,182,212,0.08)";
      ctx.lineWidth = 0.5;
      ctx.stroke();
    });

    const earthGradient = ctx.createRadialGradient(CX - 8, CY - 8, 5, CX, CY, EARTH_R);
    earthGradient.addColorStop(0, "#1a3a5c");
    earthGradient.addColorStop(0.5, "#113050");
    earthGradient.addColorStop(0.8, "#0d2640");
    earthGradient.addColorStop(1, "#061422");
    ctx.beginPath();
    ctx.arc(CX, CY, EARTH_R, 0, Math.PI * 2);
    ctx.fillStyle = earthGradient;
    ctx.fill();

    ctx.beginPath();
    ctx.arc(CX, CY, EARTH_GLOW_R, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(6,182,212,0.15)";
    ctx.lineWidth = 2;
    ctx.stroke();

    ctx.beginPath();
    ctx.arc(CX, CY, EARTH_R, 0, Math.PI * 2);
    ctx.strokeStyle = cascadeActive ? "rgba(239,68,68,0.5)" : "rgba(6,182,212,0.4)";
    ctx.lineWidth = 1;
    ctx.stroke();

    ctx.fillStyle = "rgba(255,255,255,0.4)";
    ctx.font = "7px monospace";
    ctx.textAlign = "center";
    ctx.fillText("EARTH", CX, CY + 3);

    particles.forEach((particle) => {
      if (particle.exploding) return;
      if (particle.isFragment && particle.ttl < 0) return;

      const alpha = particle.isFragment ? Math.min(1, (particle.ttl || 100) / 60) : 0.9;
      const glow = particle.isFragment ? 8 : 3;

      ctx.shadowColor = particle.color;
      ctx.shadowBlur = glow;
      ctx.beginPath();
      ctx.arc(particle.x, particle.y, particle.size, 0, Math.PI * 2);
      ctx.fillStyle = `${particle.color}${Math.floor(alpha * 255)
        .toString(16)
        .padStart(2, "0")}`;
      ctx.fill();

      if (!particle.isFragment) {
        ctx.beginPath();
        ctx.arc(particle.x, particle.y, particle.size * 1.5, 0, Math.PI * 2);
        ctx.strokeStyle = `${particle.color}33`;
        ctx.lineWidth = 1;
        ctx.stroke();
      }
    });

    ctx.shadowBlur = 0;

    if (cascadeActive && frame % 12 < 3) {
      ctx.fillStyle = "rgba(239,68,68,0.04)";
      ctx.fillRect(0, 0, W, H);
    }
  }

  function triggerCascade(particles, epicenterIdx) {
    const epicenter = particles[epicenterIdx];
    if (!epicenter) return { newFragments: [], hits: 0 };

    let hits = 0;
    const newFragments = [];

    particles.forEach((particle, index) => {
      if (index === epicenterIdx || particle.isFragment || particle.exploding) return;

      const dx = particle.x - epicenter.x;
      const dy = particle.y - epicenter.y;
      const dist = Math.sqrt(dx * dx + dy * dy);

      if (dist < 28 + Math.random() * 10) {
        particle.exploding = true;
        hits += 1;

        const fragmentCount = 5 + Math.floor(Math.random() * 6);
        for (let f = 0; f < fragmentCount; f += 1) {
          const angle = Math.random() * Math.PI * 2;
          const speed = 0.8 + Math.random() * 3.2;

          newFragments.push({
            x: particle.x,
            y: particle.y,
            angle: particle.angle,
            r: particle.r,
            orbitSpeed: particle.orbitSpeed + (Math.random() - 0.5) * 0.02,
            vx: Math.cos(angle) * speed,
            vy: Math.sin(angle) * speed,
            size: 1.4 + Math.random() * 1.4,
            color: "#ef4444",
            isFragment: true,
            exploding: false,
            ttl: 90 + Math.random() * 120,
            mass: 0.1,
            id: Math.random(),
          });
        }
      }
    });

    return { newFragments, hits };
  }

  const runSimulation = useCallback(() => {
    if (stateRef.current.running) return;
    cancelAnimationFrame(animRef.current);

    let particles = initParticles(60);
    stateRef.current = { particles, frame: 0, running: true, cascadeTriggered: false, cascadeWave: 0 };
    setSimState("running");
    setStats({ collisions: 0, fragments: 0, destroyed: 0, objects: 60 });

    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#020408";
    ctx.fillRect(0, 0, W, H);

    let totalCollisions = 0;
    let cascadeActive = false;

    function tick() {
      const state = stateRef.current;
      state.frame += 1;

      if (state.frame === 90 && !state.cascadeTriggered) {
        state.cascadeTriggered = true;
        cascadeActive = true;
        setSimState("cascade");
        const epicenterIndex = Math.floor(particles.length / 2);
        const { newFragments, hits } = triggerCascade(particles, epicenterIndex);
        totalCollisions += hits;
        particles = [...particles, ...newFragments];
        state.cascadeWave = 1;
      }

      if (cascadeActive && state.frame % 60 === 0 && state.cascadeWave < 6) {
        state.cascadeWave += 1;
        const alive = particles.filter((particle) => !particle.isFragment && !particle.exploding);
        if (alive.length > 0) {
          const target = alive[Math.floor(Math.random() * alive.length)];
          const idx = particles.indexOf(target);
          const { newFragments, hits } = triggerCascade(particles, idx);
          totalCollisions += hits;
          particles = [...particles, ...newFragments];
        }
      }

      particles = particles.filter((particle) => !(particle.isFragment && particle.ttl <= 0));
      let destroyedCount = 0;

      particles.forEach((particle) => {
        if (particle.isFragment) {
          particle.x += particle.vx;
          particle.y += particle.vy;
          particle.vx *= 0.98;
          particle.vy *= 0.98;
          particle.ttl -= 1;
          if (particle.ttl <= 0) destroyedCount += 1;
        } else if (!particle.exploding) {
          particle.angle += particle.orbitSpeed;
          particle.x = CX + Math.cos(particle.angle) * particle.r;
          particle.y = CY + Math.sin(particle.angle) * particle.r;
        }
      });

      drawScene(ctx, particles, state.frame, cascadeActive);

      const fragmentCount = particles.filter((particle) => particle.isFragment).length;
      const activeObjects = particles.filter((particle) => !particle.isFragment && !particle.exploding).length;

      setStats((prev) => ({
        collisions: totalCollisions,
        fragments: fragmentCount,
        destroyed: prev.destroyed + destroyedCount,
        objects: activeObjects,
      }));

      if (state.frame < 600) {
        animRef.current = requestAnimationFrame(tick);
      } else {
        setSimState("done");
        state.running = false;
      }
    }

    animRef.current = requestAnimationFrame(tick);
  }, []);

  function reset() {
    cancelAnimationFrame(animRef.current);
    stateRef.current = { particles: [], frame: 0, running: false, cascadeTriggered: false, cascadeWave: 0 };
    setSimState("idle");
    setStats({ collisions: 0, fragments: 0, destroyed: 0, objects: 60 });
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#020408";
    ctx.fillRect(0, 0, W, H);
    const initialParticles = initParticles(60);
    drawScene(ctx, initialParticles, 0, false);
  }

  useEffect(() => {
    const timeout = setTimeout(reset, 100);
    return () => {
      cancelAnimationFrame(animRef.current);
      clearTimeout(timeout);
    };
  }, []);

  const isIdle = simState === "idle";

  const panelWrapperStyle = { height: "100%" };
  const panelStyle = { height: "100%", display: "flex", flexDirection: "column", overflow: "hidden" };

  return (
    <div style={panelWrapperStyle}>
      <div className="panel" style={panelStyle}>
        <div className="panel-header" style={{ flexShrink: 0, alignItems: "center", gap: 8 }}>
          <div className={`status-dot ${simState === "cascade" ? "red" : simState === "running" ? "amber" : "dim"}`} />
          <span className="panel-title">Kessler Syndrome Simulator</span>
        </div>

        <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", padding: "6px 8px", gap: 5, overflow: "hidden" }}>

          <div style={{ flex: 1, minHeight: 0, position: "relative", overflow: "hidden", borderRadius: 2, border: "var(--border)" }}>
            <canvas ref={canvasRef} width={W} height={H} style={{ width: "100%", height: "100%", display: "block", aspectRatio: "1200 / 700", objectFit: "contain" }} />

            {simState === "idle" && (
              <div style={{ position: "absolute", inset: 0, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 12, background: "rgba(0, 0, 0, 0.18)", padding: 16, textAlign: "center", color: "var(--text-primary)" }}>
                <div style={{ fontFamily: "var(--font-display)", fontSize: 12, letterSpacing: "0.18em" }}>
                  READY TO START THE KESSLER CASCADE
                </div>
              </div>
            )}

            {simState === "idle" && (
              <div style={{ position: "absolute", bottom: 8, left: 0, right: 0, textAlign: "center", fontFamily: "var(--font-display)", fontSize: 7, color: "var(--text-dim)", letterSpacing: "0.12em" }}>
                PRESS START TO BEGIN
              </div>
            )}

            {simState === "done" && (
              <div style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center", background: "rgba(127,29,29,0.15)", border: "1px solid var(--red)", borderRadius: 2 }}>
                <div style={{ textAlign: "center" }}>
                  <div style={{ fontFamily: "var(--font-display)", fontSize: 11, color: "var(--red)", letterSpacing: "0.2em" }}>
                    ORBITAL SHELL COMPROMISED
                  </div>
                  <div style={{ fontFamily: "var(--font-mono)", fontSize: 8, color: "var(--text-secondary)", marginTop: 4 }}>
                    GPS · Weather Satellites · Internet from Space → OFFLINE
                  </div>
                </div>
              </div>
            )}
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: 4, flexShrink: 0 }}>
            {[
              ["OBJECTS", stats.objects, "var(--cyan)"],
              ["COLLISIONS", stats.collisions, "var(--red)"],
              ["FRAGMENTS", stats.fragments, "var(--amber)"],
              ["DESTROYED", stats.destroyed, "var(--text-secondary)"],
            ].map(([label, value, color]) => (
              <div key={label} style={{ background: "var(--surface)", border: "var(--border)", borderRadius: 2, padding: "4px 4px", textAlign: "center" }}>
                <div style={{ fontFamily: "var(--font-display)", fontSize: 13, fontWeight: 700, color, lineHeight: 1 }}>{value}</div>
                <div style={{ fontSize: 6, letterSpacing: "0.08em", color: "var(--text-dim)", marginTop: 2 }}>{label}</div>
              </div>
            ))}
          </div>

          <div style={{ display: "flex", gap: 5, flexShrink: 0 }}>
            <button
              onClick={runSimulation}
              disabled={simState === "running" || simState === "cascade"}
              style={{
                flex: 1,
                padding: "6px",
                background: simState === "idle" || simState === "done" ? "rgba(127,29,29,0.4)" : "var(--panel)",
                border: simState === "idle" || simState === "done" ? "1px solid var(--red)" : "1px solid var(--panel-border)",
                color: simState === "idle" || simState === "done" ? "var(--red)" : "var(--text-dim)",
                fontFamily: "var(--font-display)",
                fontSize: 8,
                letterSpacing: "0.18em",
                cursor: simState === "running" || simState === "cascade" ? "not-allowed" : "pointer",
                borderRadius: 2,
                transition: "all 0.2s",
              }}
            >
              {simState === "running" || simState === "cascade" ? "⬤ SIMULATING..." : "▶ SIMULATE COLLISION"}
            </button>
            <button
              onClick={reset}
              style={{ padding: "6px 10px", background: "var(--panel)", border: "var(--border)", color: "var(--text-dim)", fontFamily: "var(--font-display)", fontSize: 8, cursor: "pointer", borderRadius: 2 }}
            >
              RESET
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
