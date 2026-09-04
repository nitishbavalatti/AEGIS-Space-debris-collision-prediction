import { useEffect, useRef } from "react";

export default function AltitudeHeatmap({ altitudeBands }) {
  const svgRef = useRef(null);

  useEffect(() => {
    if (!altitudeBands || altitudeBands.length === 0 || !svgRef.current) return;

    // Dynamic import d3
    import("d3").then((d3) => {
      const el = svgRef.current;
      const W = el.clientWidth || 260;
      const H = el.clientHeight || 280;
      const margin = { top: 8, right: 48, bottom: 16, left: 44 };
      const innerW = W - margin.left - margin.right;
      const innerH = H - margin.top - margin.bottom;

      d3.select(el).selectAll("*").remove();

      const svg = d3.select(el)
        .attr("width", W)
        .attr("height", H);

      const g = svg.append("g")
        .attr("transform", `translate(${margin.left},${margin.top})`);

      const maxCount = d3.max(altitudeBands, d => d.count) || 1;

      // Scales
      const xScale = d3.scaleLinear()
        .domain([0, maxCount])
        .range([0, innerW]);

      const yScale = d3.scaleBand()
        .domain(altitudeBands.map(d => d.alt_mid))
        .range([innerH, 0])
        .padding(0.15);

      // Color scale — cyan for normal, amber for dense
      const colorScale = (count) => {
        const ratio = count / maxCount;
        if (ratio > 0.7) return "#ef4444";
        if (ratio > 0.4) return "#f59e0b";
        if (ratio > 0.2) return "#06b6d4";
        return "#0d3a4a";
      };

      // Grid lines
      g.append("g")
        .attr("class", "grid")
        .selectAll("line")
        .data(xScale.ticks(4))
        .join("line")
        .attr("x1", d => xScale(d))
        .attr("x2", d => xScale(d))
        .attr("y1", 0)
        .attr("y2", innerH)
        .attr("stroke", "#0f2035")
        .attr("stroke-width", 1);

      // Bars
      g.selectAll(".bar")
        .data(altitudeBands)
        .join("rect")
        .attr("class", "bar")
        .attr("x", 0)
        .attr("y", d => yScale(d.alt_mid))
        .attr("width", 0)
        .attr("height", yScale.bandwidth())
        .attr("fill", d => colorScale(d.count))
        .attr("rx", 1)
        .transition()
        .duration(800)
        .ease(d3.easeQuadOut)
        .delay((d, i) => i * 8)
        .attr("width", d => xScale(d.count));

      // Count labels
      g.selectAll(".count-label")
        .data(altitudeBands.filter(d => d.count > 0))
        .join("text")
        .attr("class", "count-label")
        .attr("x", d => xScale(d.count) + 3)
        .attr("y", d => yScale(d.alt_mid) + yScale.bandwidth() / 2 + 3)
        .attr("fill", "#7da8c8")
        .attr("font-family", "JetBrains Mono, monospace")
        .attr("font-size", 7)
        .text(d => d.count);

      // Altitude axis (left)
      const yAxis = g.append("g");
      altitudeBands.filter((_, i) => i % 4 === 0).forEach(d => {
        yAxis.append("text")
          .attr("x", -4)
          .attr("y", yScale(d.alt_mid) + yScale.bandwidth() / 2 + 3)
          .attr("text-anchor", "end")
          .attr("fill", "#3a5a78")
          .attr("font-family", "JetBrains Mono, monospace")
          .attr("font-size", 7)
          .text(`${d.alt_lower}`);
      });

      // Landmark lines
      const landmarks = [
        { alt: 412, label: "ISS", color: "#06b6d4" },
        { alt: 550, label: "Starlink", color: "#3b82f6" },
        { alt: 800, label: "DENSE", color: "#f59e0b" },
      ];

      landmarks.forEach(({ alt, label, color }) => {
        const band = altitudeBands.find(b => b.alt_lower <= alt && b.alt_upper > alt);
        if (!band) return;
        const y = yScale(band.alt_mid) + yScale.bandwidth() / 2;
        g.append("line")
          .attr("x1", 0).attr("x2", innerW)
          .attr("y1", y).attr("y2", y)
          .attr("stroke", color)
          .attr("stroke-width", 1)
          .attr("stroke-dasharray", "3,2")
          .attr("opacity", 0.6);
        g.append("text")
          .attr("x", innerW + 2)
          .attr("y", y + 3)
          .attr("fill", color)
          .attr("font-family", "JetBrains Mono, monospace")
          .attr("font-size", 7)
          .text(label);
      });
    });
  }, [altitudeBands]);

  return (
    <div className="panel" style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <div className="panel-header">
        <div className="status-dot dim" />
        <span className="panel-title">Orbital Altitude Density</span>
      </div>
      <div style={{ flex: 1, padding: "8px 4px 4px 4px", overflow: "hidden", position: "relative" }}>
        <div style={{ fontSize: 7, letterSpacing: "0.15em", color: "var(--text-dim)", padding: "0 8px 4px", display: "flex", justifyContent: "space-between" }}>
          <span>200 km</span>
          <span>ALTITUDE BAND</span>
          <span>2000 km</span>
        </div>
        <svg ref={svgRef} style={{ width: "100%", height: "calc(100% - 20px)" }} />
      </div>
    </div>
  );
}
