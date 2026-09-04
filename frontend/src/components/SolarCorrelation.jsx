import { useEffect, useRef, useState } from "react";

// Robust max: ignore extreme outliers when sizing an axis so a single bad
// sample (e.g. a sensor glitch that slips past backend validation) can't
// single-handedly stretch the scale and squash all the real data flat.
// Falls back to plain max when there isn't enough data for percentiles to
// mean anything.
function robustMax(values, percentile = 0.99) {
  const clean = values.filter((v) => v != null && !Number.isNaN(v));
  if (clean.length === 0) return 0;
  if (clean.length < 20) return Math.max(...clean);
  const sorted = [...clean].sort((a, b) => a - b);
  const idx = Math.min(sorted.length - 1, Math.floor(sorted.length * percentile));
  return sorted[idx];
}

export default function SolarCorrelation({ correlationSeries, latestSensor, solarSummary }) {
  const containerRef = useRef(null);
  const svgRef = useRef(null);
  const [dims, setDims] = useState({ w: 0, h: 0 });

  // ── Anti-flicker gatekeeper ─────────────────────────────────────────────
  // correlationSeries arrives fresh on every poll. Most polls are fine, but
  // occasionally the backend returns a genuinely SHORTER/STALER response
  // than the previous one — e.g. NOAA's geomag cache (ttl=60s in
  // noaa_swpc.py) racing its own refresh mid-request, or a transient
  // upstream hiccup that momentarily returns fewer valid rows. Rendering
  // that update directly is what causes the chart to visibly shrink or
  // distort for a few seconds before self-correcting on the NEXT poll once
  // NOAA/the cache catches back up — the "cuts off, then renders full
  // again" symptom.
  //
  // Fix: don't trust every incoming update. Only replace what's on screen
  // (`displaySeries`) when the new data is a genuine improvement — either
  // it has at least as many points, or it reaches further forward in time
  // than what's already displayed. A response that's BOTH shorter AND not
  // newer is exactly what a transient partial/stale fetch looks like;
  // reject it and keep showing the last good render. The next real poll
  // (a few seconds later) either brings a genuinely fresher/fuller series
  // (accepted normally) or repeats the same good state (no visible
  // change either way).
  const [displaySeries, setDisplaySeries] = useState(null);
  const lastGoodMaxTsRef = useRef(null);

  useEffect(() => {
    if (!correlationSeries || correlationSeries.length < 2) {
      // Only accept an empty/too-short update if we don't already have a
      // good render to protect — a momentary empty response shouldn't wipe
      // out an already-good chart and flash the "AWAITING GEOMAG DATA"
      // placeholder before flipping straight back.
      if (!lastGoodMaxTsRef.current) {
        setDisplaySeries(correlationSeries);
      }
      return;
    }

    // Cheap max-timestamp check — NOAA's time_tag strings sort correctly
    // lexicographically (ISO-ish, consistent format from a single source),
    // no need to parse Dates just for this comparison.
    let newMaxTs = "";
    for (const d of correlationSeries) {
      if (d?.timestamp && d.timestamp > newMaxTs) newMaxTs = d.timestamp;
    }
    const newLen = correlationSeries.length;
    const prevMaxTs = lastGoodMaxTsRef.current;
    const prevLen = displaySeries?.length ?? 0;

    // Reject two cases: the live edge itself moving backward, or a stall
    // with fewer points. This used to be: reject only when a response
    // was BOTH shorter AND not newer (equivalently: accept if longer, OR
    // if further forward in time). That left a gap — a response with
    // MORE total points than what's on screen (e.g. the backend's locked
    // history keeps growing from backfill) but a max timestamp that's
    // gone backward — its live tail briefly missing, e.g. a source going
    // stale in noaa_swpc.py's freshness check, or an out-of-order network
    // response from an earlier poll resolving after a later one — still
    // got *accepted*, because "more points" alone was enough under the
    // old rule. Rendering that pulls the chart's live edge (dataMax)
    // backward relative to what's already on screen is the same visible
    // regression this gatekeeper exists to stop, just triggered from the
    // other side.
    //
    // Deliberately NOT tightened to "newLen must never decrease": the
    // series is a sliding window (minutes_back from now), so a point
    // aging out of the tail while nothing new has landed yet at the head
    // is normal, healthy, and roughly as frequent as gaining one — that's
    // the original "shorter but further forward" tolerance, and it's
    // kept. What's newly rejected is specifically newMaxTs going
    // backward, plus the original same-timestamp-but-shorter stall.
    const timeRegressed = newMaxTs < prevMaxTs;
    const stalledWithFewerPoints = newMaxTs === prevMaxTs && newLen < prevLen;
    const looksLikeRegression = prevMaxTs && (timeRegressed || stalledWithFewerPoints);

    if (looksLikeRegression) {
      return; // keep showing the last good render, wait for the next poll
    }

    lastGoodMaxTsRef.current = newMaxTs || prevMaxTs;
    setDisplaySeries(correlationSeries);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [correlationSeries]);

  // ResizeObserver
  useEffect(() => {
    if (!containerRef.current) return;
    const ro = new ResizeObserver((entries) => {
      const { width, height } = entries[0].contentRect;
      if (width > 10 && height > 10) setDims({ w: width, h: height });
    });
    ro.observe(containerRef.current);
    const rect = containerRef.current.getBoundingClientRect();
    if (rect.width > 10 && rect.height > 10) setDims({ w: rect.width, h: rect.height });
    return () => ro.disconnect();
  }, []);

  // D3 render
  useEffect(() => {
    if (!svgRef.current || dims.w < 10 || dims.h < 10) return;
    if (!displaySeries || displaySeries.length < 2) return;

    import("d3").then((d3) => {
      const el = svgRef.current;
      const W = dims.w;
      const H = dims.h;
      const margin = { top: 12, right: 22, bottom: 24, left: 50 }; // right widened 16→22 to fit the new risk axis's tick labels
      const innerW = W - margin.left - margin.right;
      const innerH = H - margin.top - margin.bottom;
      if (innerW <= 0 || innerH <= 0) return;

      d3.select(el).selectAll("*").remove();
      const svg = d3.select(el).attr("width", W).attr("height", H);
      const g = svg.append("g").attr("transform", `translate(${margin.left},${margin.top})`);

      // NOAA SWPC timestamps (e.g. "2026-08-12 15:59:00.000") are NOT
      // strict ISO-8601 — they use a space instead of "T" and have no
      // trailing "Z". d3.isoParse rejects that format and returns null,
      // which used to silently fall back to `new Date()` for EVERY row,
      // collapsing all points onto the same x-coordinate (the graph
      // looked "empty" because the line/area had zero width). Normalize
      // the string first, and drop rows that still fail to parse instead
      // of faking a timestamp for them.
      const data = displaySeries.slice(-1440).map((d) => {
        const raw = d?.timestamp;
        let parsedDate = null;
        if (raw) {
          const normalized = raw.includes("T") ? raw : `${raw.replace(" ", "T")}Z`;
          const dt = new Date(normalized);
          if (!isNaN(dt.getTime())) parsedDate = dt;
        }
        return { ...d, parsedDate };
      }).filter((d) => d.parsedDate);

      if (data.length < 2) return;

      // Gap detection — NOAA's feed is supposed to publish ~once/minute,
      // but genuinely has outage windows where it reports nothing for a
      // stretch (minutes to hours). 10 minutes matches
      // STALE_SOURCE_THRESHOLD_MINUTES on the backend (noaa_swpc.py) — a
      // real feed shouldn't naturally gap wider than that, so treat
      // anything wider as "no data here", not "interpolate across it".
      // Defined up here (rather than down where it's used for the line's
      // `defined()` and the isolated-point dot) because the domain calc
      // right below needs it too.
      const GAP_THRESHOLD_MS = 10 * 60 * 1000;
      const isContiguous = (d, i, arr) =>
        i === 0 || (d.parsedDate - arr[i - 1].parsedDate) <= GAP_THRESHOLD_MS;

      // Split the series into contiguous runs — a new run starts every
      // time a gap wider than GAP_THRESHOLD_MS appears. segIndex[i] says
      // which run data[i] belongs to.
      //
      // The domain used to be sized from "everything except a single
      // isolated straggler" (a point whose neighbors on BOTH sides were
      // too far away). That correctly stopped ONE stray point — e.g. a
      // fresh live reading landing ahead of a historical batch that
      // hasn't caught up yet — from single-handedly dragging xMax forward
      // and squeezing the real dense history into a sliver.
      //
      // It did NOT stop a small CLUSTER of 2+ fresh points doing the same
      // thing: two or three new points a few minutes apart are each
      // "close to a neighbor" (their neighbor within the cluster), so the
      // old single-point isolation check was false for all of them even
      // though the cluster as a whole sits across a multi-hour gap from
      // the main body — same "everything smushes together" artifact,
      // just triggered by a 2-5-point run instead of a 1-point run. This
      // is the more likely real-world trigger, not a rarer edge case: a
      // source going stale and un-stale again (see
      // STALE_SOURCE_THRESHOLD_MINUTES in noaa_swpc.py) or a locked
      // buffer still backfilling after a restart naturally produces a
      // *few* fresh points ahead of a gap, not just one.
      //
      // Fix: size the domain from the LARGEST contiguous run, whatever
      // its length, rather than "everything but literal solo points". A
      // single straggler is just a run of length 1 — it never wins
      // against a real historical body, so the old protected case falls
      // out of this as a special case for free. A small fresh cluster now
      // loses the same way, right up until it's genuinely accumulated
      // into the bigger run — at which point it SHOULD anchor the
      // domain, because it's no longer a stray, it's the current data.
      // Ties go to the LATER run, matching this component's existing bias
      // toward trusting forward/current data over historical data.
      const segIndex = new Array(data.length);
      const segments = [];
      data.forEach((d, i) => {
        if (i === 0 || !isContiguous(d, i, data)) segments.push([]);
        segIndex[i] = segments.length - 1;
        segments[segIndex[i]].push(d);
      });
      let mainSegIdx = 0;
      for (let i = 1; i < segments.length; i++) {
        if (segments[i].length >= segments[mainSegIdx].length) mainSegIdx = i;
      }
      // Guard against a degenerate (zero-width) domain in the pathological
      // case where even the largest run is a single point — fall back to
      // the full unfiltered series rather than collapsing xMin===xMax.
      const domainSource = segments[mainSegIdx].length >= 2 ? segments[mainSegIdx] : data;

      const now = new Date();
      const dataMax = d3.max(domainSource, (d) => d.parsedDate);
      const xMax = dataMax && dataMax < now ? dataMax : now;

      // Hard ceiling as a second line of defense: never let the domain
      // reach back further than the expected window (last 1440 minutes ≈
      // 24h) behind xMax, in case domainSource is still unreasonably
      // spread out (e.g. two separate contiguous clusters far apart).
      const MAX_WINDOW_MS = 25 * 60 * 60 * 1000; // 24h of data + 1h slack
      const rawXMin = d3.min(domainSource, (d) => d.parsedDate);
      const xMin = rawXMin && xMax - rawXMin > MAX_WINDOW_MS
        ? new Date(xMax.getTime() - MAX_WINDOW_MS)
        : rawXMin;
      const xScale = d3.scaleTime()
        .domain([xMin, xMax])
        .range([0, innerW]);

      // mag_total is IMF Bt in nT (real DSCOVR/SOLAR1 data), not fake µT.
      // Use a robust (p99-clipped) max so one leftover outlier value can't
      // blow out the scale and flatten all the real ~1-10nT variation.
      //
      // IMPORTANT: robustMax only decides the AXIS RANGE. It does nothing
      // to the actual plotted values — the line/area generators below
      // still draw every point's true mag_total, including the ~1% that
      // sit above this p99 cutoff. Without `.clamp(true)` on the scale,
      // those points map to y-pixels above 0 (i.e. negative), which is
      // above the plot's top edge with nothing to stop them — that's the
      // near-vertical line shooting off the top of the chart. `.clamp`
      // pins any value outside the domain to the nearest edge of the
      // range instead of extrapolating past it. A clip-path (applied to
      // the drawn paths further down) is the second half of the fix, so
      // that clamped points are also visually cut off at the panel
      // boundary rather than the path being free to draw wherever SVG
      // lets it once bent back from clamped Ys.
      //
      // Sourced from domainSource (the visible run), NOT the full `data`
      // array. This used to read from `data`, which meant a chunk of
      // points sitting in a run the X-axis has scoped OUT of view (see
      // the segment-selection logic above) could still stretch the
      // Y-axis to fit them — a value that's clipped off-screen
      // horizontally has no visible bar to fall inside, so it was
      // effectively invisible EXCEPT for its distorting effect on the
      // scale. Concretely: an off-screen run with a ~50nT reading in it
      // pushes the axis out to a 0-60nT range, and the real, currently-
      // visible ~3-7nT values — genuinely varying, correctly drawn — get
      // compressed into a thin band clinging to the bottom, reading as
      // "the line went to zero" even though nothing about the actual
      // visible data changed. p99-clipping alone doesn't catch this: a
      // handful of points from one off-screen run is easily >1% of a
      // long window, so they survive the percentile cut and still land
      // as the domain's max. Sizing off domainSource instead means the
      // axis only ever fits what's actually drawn; an extreme value in
      // an excluded run gets clamped at the edge if it happens to render
      // as an isolated-run dot within the X range, same as any other
      // robustMax outlier, rather than dictating the whole scale.
      const magValues = domainSource.map((d) => d?.mag_total ?? 0);
      const rawMagMin = d3.min(magValues) ?? 0;
      const rawMagMax = robustMax(magValues);
      const magSpread = rawMagMax - rawMagMin;
      const magPad = magSpread < 0.5 ? 1 : magSpread * 0.1;
      const yMag = d3.scaleLinear()
        .domain([Math.max(0, rawMagMin - magPad), rawMagMax + magPad])
        .nice()
        .range([innerH, 0])
        .clamp(true);

      // (GAP_THRESHOLD_MS / isContiguous are defined earlier, above the
      // X-domain calc, since the domain needs them too. Still used below
      // for the line/area `defined()` checks.)

      // Risk score gets its own right-hand axis and scale rather than
      // sharing yMag — plotting it against the Bt scale would produce a
      // misleading scale-crossing between two unrelated units (nT vs a
      // 0–100 score), which is why risk was kept off this chart entirely
      // before. Domain is fixed to [0, 100] rather than data-driven so the
      // axis doesn't rescale/jump from poll to poll — 100 already means
      // something concrete here (it lines up with the CRITICAL/HIGH/
      // MEDIUM/LOW thresholds on the debris legend), so clamping to a
      // fixed domain is more meaningful than a tight-fit one would be.
      const yRisk = d3.scaleLinear()
        .domain([0, 100])
        .range([innerH, 0])
        .clamp(true);

      // Clip path — hard boundary at the plot area edges. Belt-and-
      // suspenders alongside `.clamp(true)` above: clamp stops values
      // from mapping outside [0, innerH] in the first place, the clip
      // path guarantees nothing drawn (area fill, stroke) can ever
      // render outside the panel regardless of how the scale/curve
      // interpolation behaves near a clamped point.
      const defs = svg.append("defs");
      defs.append("clipPath")
        .attr("id", "plotClip")
        .append("rect")
        .attr("width", innerW)
        .attr("height", innerH);

      // Storm shading (Kp-based, from NOAA)
      data.forEach((d, i) => {
        if (d?.storm_active) {
          const x1 = xScale(d.parsedDate);
          const x2 = i < data.length - 1 ? xScale(data[i + 1].parsedDate) : x1 + 4;
          g.append("rect")
            .attr("x", x1).attr("y", 0)
            .attr("width", Math.max(2, x2 - x1))
            .attr("height", innerH)
            .attr("fill", "rgba(239,68,68,0.07)");
        }
      });

      // Grid
      g.append("g").selectAll("line").data(yMag.ticks(4)).join("line")
        .attr("x1", 0).attr("x2", innerW)
        .attr("y1", (d) => yMag(d)).attr("y2", (d) => yMag(d))
        .attr("stroke", "#0f2035").attr("stroke-width", 1);

      // Gradient
      const grad = defs.append("linearGradient")
        .attr("id", "magGrad2").attr("x1", 0).attr("y1", 0).attr("x2", 0).attr("y2", 1);
      grad.append("stop").attr("offset", "0%").attr("stop-color", "#f59e0b").attr("stop-opacity", 0.30);
      grad.append("stop").attr("offset", "100%").attr("stop-color", "#f59e0b").attr("stop-opacity", 0.02);

      const magArea = d3.area()
        .x((d) => xScale(d.parsedDate))
        .y0(yMag(yMag.domain()[0]))
        .y1((d) => yMag(d?.mag_total ?? 0))
        .curve(d3.curveMonotoneX)
        .defined(isContiguous);

      const magLine = d3.line()
        .x((d) => xScale(d.parsedDate))
        .y((d) => yMag(d?.mag_total ?? 0))
        .curve(d3.curveMonotoneX)
        .defined(isContiguous);

      // Confirmed via console: only ~23/1435 rows currently carry a
      // risk_score — scoring has only been running for ~23 minutes, and
      // the rest of this 1440-minute window predates it entirely.
      //
      // A previous version of this fill seeded lastKnownRisk with that
      // EARLIEST real score and held it backward across the whole
      // window, on the theory that it would make "the line span the
      // full chart instead of only its last sliver." On screen that's
      // exactly what it did NOT fix: filledRisk became non-null
      // everywhere, but the only pixels showing real, varying data were
      // still that same ~1.5%-wide sliver at the right edge — now
      // preceded by ~23 hours of a flat value nothing ever measured.
      // That reads as "risk sat at ~80 all day," which is false, and on
      // a chart whose whole point is to correlate risk against Bt it's
      // actively misleading whenever Bt is visibly moving and the risk
      // line visibly isn't (i.e. most of the time, by construction).
      //
      // Forward-fill (hold the last REAL value) is still correct for
      // bridging gaps *within* the scored window — that's normal
      // handling for a metric computed on a slower cadence than the main
      // poll. What was wrong is inventing a value for before the metric
      // existed. lastKnownRisk now starts at null and stays null until
      // the first real score, so filledRisk is null there too —
      // `defined()` below correctly leaves that period undrawn instead
      // of drawing history that was never measured. The real segment is
      // only ever as wide as the real history, and grows on its own as
      // more of it accumulates.
      let lastKnownRisk = null;
      const filledRisk = data.map((d) => {
        if (d?.risk_score != null) lastKnownRisk = d.risk_score;
        return lastKnownRisk;
      });

      // curveLinear (not curveMonotoneX like magLine above) so it draws
      // straight segments and reads as visually distinct from the
      // smoothed Bt line even before the dash pattern is applied.
      // `defined` still respects isContiguous (same gate magLine uses)
      // so the line still visually breaks across a genuine multi-hour
      // NOAA outage rather than bridging it with a straight connector —
      // held-value filling covers missing SCORES, not missing DATA.
      const riskLine = d3.line()
        .x((d) => xScale(d.parsedDate))
        .y((d, i) => yRisk(filledRisk[i] ?? 0))
        .curve(d3.curveLinear)
        .defined((d, i, arr) => isContiguous(d, i, arr) && filledRisk[i] != null);

      g.append("path").datum(data).attr("fill", "url(#magGrad2)").attr("d", magArea)
        .attr("clip-path", "url(#plotClip)");
      g.append("path").datum(data)
        .attr("fill", "none").attr("stroke", "#f59e0b")
        .attr("stroke-width", 1.8)
        .attr("d", magLine)
        .attr("clip-path", "url(#plotClip)");

      // Points belonging to any run OTHER than the one the domain was
      // sized from (mainSegIdx) — a real reading, but one the line
      // generators can't draw anything touching, either because it's a
      // true solo straggler or because its whole run lost out to a
      // bigger one above. Most of these fall outside [xMin, xMax] and
      // simply won't render (clipped/out of range); any that land near
      // the domain edge get a small dot instead of silently vanishing.
      g.selectAll(".mag-isolated-point")
        .data(data.filter((d, i) => segIndex[i] !== mainSegIdx && d?.mag_total != null))
        .join("circle")
        .attr("class", "mag-isolated-point")
        .attr("cx", (d) => xScale(d.parsedDate))
        .attr("cy", (d) => yMag(d.mag_total))
        .attr("r", 2.2)
        .attr("fill", "#f59e0b")
        .attr("clip-path", "url(#plotClip)");

      // Risk line — dotted red, plotted against yRisk (right axis).
      // Drawn after the Bt area/line so it layers on top and isn't
      // visually buried under the amber fill.
      g.append("path").datum(data)
        .attr("fill", "none").attr("stroke", "#ef4444")
        .attr("stroke-width", 1.2)
        .attr("stroke-dasharray", "2,3")
        .attr("d", riskLine)
        .attr("clip-path", "url(#plotClip)");

      // Same off-main-run problem as mag_total (see .mag-isolated-point
      // above), and more likely to actually bite here now that the fill
      // no longer backdates a value across the whole window: the first
      // real score can easily be a single point sitting right at the
      // isContiguous boundary between "no data" and "scored." A real
      // reading with no defined neighbor on either side gets dropped by
      // the line generator (it needs 2+ consecutive defined points to
      // draw anything) and would otherwise disappear with nothing to
      // mark it — the exact vanishing-point bug already fixed for mag,
      // just not yet ported over to risk.
      g.selectAll(".risk-isolated-point")
        .data(data.filter((d, i) => segIndex[i] !== mainSegIdx && d?.risk_score != null))
        .join("circle")
        .attr("class", "risk-isolated-point")
        .attr("cx", (d) => xScale(d.parsedDate))
        .attr("cy", (d) => yRisk(d.risk_score))
        .attr("r", 1.8)
        .attr("fill", "#ef4444")
        .attr("clip-path", "url(#plotClip)");

      // X axis
      g.append("g").attr("transform", `translate(0,${innerH})`)
        .call(d3.axisBottom(xScale).ticks(5).tickFormat(d3.timeFormat("%H:%M")))
        .call((a) => {
          a.selectAll("text").attr("fill", "#3a5a78").attr("font-size", 7).attr("font-family", "JetBrains Mono, monospace");
          a.select(".domain").attr("stroke", "#0f2035");
          a.selectAll(".tick line").attr("stroke", "#0f2035");
        });

      // Left Y axis (IMF Bt, nT)
      g.append("g")
        .call(d3.axisLeft(yMag).ticks(4).tickFormat((d) => `${Number(d).toFixed(1)}nT`))
        .call((a) => {
          a.selectAll("text").attr("fill", "#92600a").attr("font-size", 7).attr("font-family", "JetBrains Mono, monospace");
          a.select(".domain").attr("stroke", "#0f2035");
          a.selectAll(".tick line").attr("stroke", "#0f2035");
        });

      // Right Y axis (Risk score, 0–100) — same styling convention as the
      // left axis: tick text in a darker shade of the series' line color
      // (#b91c1c here vs. riskLine's #ef4444, mirroring #92600a vs.
      // magLine's #f59e0b above).
      g.append("g")
        .attr("transform", `translate(${innerW},0)`)
        .call(d3.axisRight(yRisk).ticks(4))
        .call((a) => {
          a.selectAll("text").attr("fill", "#b91c1c").attr("font-size", 7).attr("font-family", "JetBrains Mono, monospace");
          a.select(".domain").attr("stroke", "#0f2035");
          a.selectAll(".tick line").attr("stroke", "#0f2035");
        });

      if (magSpread < 0.5) {
        g.append("text")
          .attr("x", innerW / 2).attr("y", innerH / 2)
          .attr("text-anchor", "middle")
          .attr("fill", "#f59e0b").attr("opacity", 0.35)
          .attr("font-size", 8).attr("font-family", "JetBrains Mono, monospace")
          .text("IMF Bt near constant — quiet solar wind");
      }

      // Now that the risk fill no longer invents pre-scoring history, a
      // freshly-started scorer will legitimately paint red across only a
      // small recent slice of the window — call that out explicitly so
      // it reads as "scoring just started" rather than "the line broke
      // again." Placed top-right so it never collides with the mag
      // message above, which only ever appears centered.
      const firstScoredIdx = data.findIndex((d) => d?.risk_score != null);
      const windowMs = data[data.length - 1].parsedDate - data[0].parsedDate;
      const scoredSpanMs = firstScoredIdx >= 0
        ? data[data.length - 1].parsedDate - data[firstScoredIdx].parsedDate
        : 0;
      if (windowMs > 0 && scoredSpanMs / windowMs < 0.15) {
        g.append("text")
          .attr("x", innerW - 4).attr("y", 10)
          .attr("text-anchor", "end")
          .attr("fill", "#ef4444").attr("opacity", 0.45)
          .attr("font-size", 7).attr("font-family", "JetBrains Mono, monospace")
          .text(firstScoredIdx >= 0 ? "RISK SCORING RECENTLY STARTED" : "NO RISK SCORE DATA YET");
      }
    });
  }, [displaySeries, dims]);

  const magTotal = solarSummary?.geomag_bt_nT ?? null;
  const magBz     = solarSummary?.geomag_bz_nT ?? null;
  const rfNoise  = latestSensor?.rf?.noise_dbm ?? null;
  // Kp/storm now come from real NOAA data (solarSummary), not the mock
  // RF-derived kp_proxy. With USE_MOCK_SENSOR=false and no RTL-SDR
  // hardware attached, latestSensor.solar is never populated, so these
  // must not depend on it — otherwise Kp/STORM permanently show "—".
  const kpIndex  = solarSummary?.max_kp_index ?? latestSensor?.solar?.kp_proxy ?? null;
  const currentStormActive = (solarSummary?.storm_level && solarSummary.storm_level !== "NONE")
    || (latestSensor?.solar?.storm_active ?? false);
  const currentStormLevel  = solarSummary?.storm_level
    || latestSensor?.solar?.alert_level
    || (currentStormActive ? "MINOR" : "NONE");
  const geomagReachable = solarSummary?.geomag_reachable ?? false;

  const stormColor =
    currentStormLevel === "NONE" ? "var(--green)"
    : currentStormLevel === "MINOR"    ? "var(--cyan)"
    : currentStormLevel === "MODERATE" ? "var(--amber)"
    : currentStormLevel === "STRONG"   ? "#fb923c"
    : "var(--red)";

  const headerAlert = currentStormActive || (solarSummary?.storm_level && solarSummary.storm_level !== "NONE");
  const hasData = displaySeries && displaySeries.length >= 2;

  // Latest real risk_score from the series — null until the backend's
  // history buffer has accumulated at least one sample (see solar.py
  // record_risk_score / _nearest_risk_score). Shown as a plain stat, not
  // plotted against Bt directly here, so it can't produce a misleading
  // scale-crossing on its own.
  const latestRisk = hasData
    ? [...displaySeries].reverse().find((d) => d?.risk_score != null)?.risk_score ?? null
    : null;

  return (
    <div className="panel" style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <div className="panel-header" style={{ flexShrink: 0 }}>
        <div className={`status-dot ${headerAlert ? "red" : "dim"}`} />
        <span className="panel-title">Solar Storm → Debris Risk Correlation</span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 10 }}>
          <Legend color="#f59e0b" label="IMF Bt (nT)" />
          <Legend color="#ef4444" label="Risk" dotted />
        </div>
      </div>

      <div style={{ flex: 1, minHeight: 0, padding: "6px 8px", display: "flex", flexDirection: "column", gap: 5, overflow: "hidden" }}>
        <div ref={containerRef} style={{ flex: 1, minHeight: 0, position: "relative", width: "100%" }}>
          {!hasData && (
            <div style={{
              position: "absolute", inset: 0, display: "flex", alignItems: "center",
              justifyContent: "center", flexDirection: "column", gap: 4,
              color: "var(--text-dim)", fontSize: 8, letterSpacing: "0.12em",
            }}>
              <span style={{ opacity: 0.4 }}>◌</span>
              <span>{geomagReachable === false ? "NOAA SWPC UNREACHABLE" : "AWAITING GEOMAG DATA"}</span>
            </div>
          )}
          <svg ref={svgRef} style={{ width: "100%", height: "100%", display: hasData ? "block" : "none" }} />
        </div>

        {/* Live readouts */}
        <div style={{ display: "flex", gap: 8, flexShrink: 0 }}>
          {[
            { label: "IMF Bt", value: magTotal != null ? `${Number(magTotal).toFixed(2)} nT` : "—", color: "var(--amber)" },
            { label: "IMF Bz", value: magBz != null ? `${Number(magBz).toFixed(2)} nT` : "—", color: magBz != null && magBz < 0 ? "var(--red)" : "var(--green)" },
            { label: "RISK",   value: latestRisk != null ? Number(latestRisk).toFixed(1) : "—", color: "var(--red)" },
            { label: "Kp INDEX",  value: kpIndex  != null ? Number(kpIndex).toFixed(2) : "—", color: currentStormActive ? "var(--red)" : "var(--green)" },
            { label: "STORM",     value: currentStormLevel, color: stormColor },
          ].map(({ label, value, color }) => (
            <div key={label} style={{ flex: 1, background: "var(--surface)", border: "var(--border)", borderRadius: 2, padding: "4px 6px" }}>
              <div style={{ fontSize: 7, letterSpacing: "0.1em", color: "var(--text-dim)" }}>{label}</div>
              <div style={{ fontFamily: "var(--font-display)", fontSize: 10, color, letterSpacing: "0.05em" }}>{value}</div>
            </div>
          ))}
        </div>


      </div>
    </div>
  );
}

function Legend({ color, label, dashed, dotted }) {
  const dash = dotted ? "2,3" : dashed ? "4,2" : undefined;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
      <svg width={16} height={8}>
        <line x1={0} y1={4} x2={16} y2={4} stroke={color} strokeWidth={1.5} strokeDasharray={dash} />
      </svg>
      <span style={{ fontSize: 7, color: "var(--text-dim)", letterSpacing: "0.1em" }}>{label}</span>
    </div>
  );
}