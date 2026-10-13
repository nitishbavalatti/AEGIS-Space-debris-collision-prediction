/**
 * useAegis — central data hook
 * Manages WebSocket connection + REST polling for all dashboard panels.
 * All components import from here — single source of truth.
 */
import { useState, useEffect, useRef, useCallback } from "react";
import { io } from "socket.io-client";
import axios from "axios";

const API = "/api";
const SOCKET_URL = "/";

function normalizeSensorSource(source) {
  if (!source) return "mock";
  return source === "esp32" || source === "hardware" ? "hardware" : "mock";
}

function isHardwareSensor(source) {
  return normalizeSensorSource(source) === "hardware";
}

export function useAegis() {
  const socketRef = useRef(null);

  const [connected, setConnected] = useState(false);
  const [sensorSource, setSensorSource] = useState("mock");

  // Debris state
  const [debrisObjects, setDebrisObjects] = useState([]);
  const [debrisStart, setDebrisStart] = useState(0);
  const [topThreats, setTopThreats] = useState([]);
  const [altitudeBands, setAltitudeBands] = useState([]);
  const [debrisCount, setDebrisCount] = useState(0);
  // Globe draws at most 2000 entities, so ask for the 2000 highest-risk objects
  // (sort=risk) instead of the first 500 in catalogue order.
  const DEBRIS_PAGE_SIZE = 2000;
  const [tierCounts, setTierCounts] = useState(null);      // whole-catalogue {critical, high}
  const [debrisIsMock, setDebrisIsMock] = useState(false);  // true while showing demo objects
  
  // ISS state
  const [rawIssPosition, setRawIssPosition] = useState(null);
  const [issPosition, setIssPosition] = useState(null);
  const [issProximity, setIssProximity] = useState(null);
  const issInterpolRef = useRef(null);

  // Conjunction state (debris×ISS + debris×debris) — previously never
  // fetched, so CollisionPopup always received `undefined` and could not show.
  const [conjunctions, setConjunctions] = useState([]);
  const [lastScreened, setLastScreened] = useState(null);
  const [conjunctionsScreening, setConjunctionsScreening] = useState(false);  // a screening run is in progress
  const [sensorDetail, setSensorDetail] = useState(null);                      // "rtlsdr" | "esp32+rtlsdr" | ...
  const [conjunctionsLoading, setConjunctionsLoading] = useState(true);

  // Solar / sensor state
  const [solarSummary, setSolarSummary] = useState(null);
  const [latestSensor, setLatestSensor] = useState(null);
  const [correlationSeries, setCorrelationSeries] = useState([]);
  // socketSolarAlert is only set by REAL hardware WebSocket push events
  const [socketSolarAlert, setSocketSolarAlert] = useState(null);

  // Overall risk
  const [currentRisk, setCurrentRisk] = useState(0);

  // ── Use a ref for latestSensor so fetchSolar never needs it as a dep ────────
  const latestSensorRef = useRef(null);
  useEffect(() => {
    latestSensorRef.current = latestSensor;
  }, [latestSensor]);

  // ─── REST fetchers ─────────────────────────────────────────────────────────
  const fetchDebris = useCallback(async (reset = false) => {
    const start = reset ? 0 : debrisStart;
    const [listRes, threatRes, heatRes] = await Promise.allSettled([
      axios.get(`${API}/debris/list?start=${start}&limit=${DEBRIS_PAGE_SIZE}&sort=mixed`),
      axios.get(`${API}/debris/top-threats?n=15`),
      axios.get(`${API}/debris/heatmap`),
    ]);

    // ── Resolve threats first so we can merge them into debrisObjects ─────────
    // Top-threat objects may rank outside the first 500 list objects.
    // If Globe never spawned an entity for them, clicking a feed row does nothing
    // because noradMapRef.get(id) returns undefined.
    // Fix: append any missing threat objects to debrisObjects so Globe always
    // has an entity for every row shown in ThreatFeed.
    const threats = threatRes.status === "fulfilled"
      ? (threatRes.value.data.threats || [])
      : [];

    if (threatRes.status === "fulfilled") {
      setTopThreats(threats);
    } else {
      console.error("Top threats fetch failed:", threatRes.reason);
    }

    if (listRes.status === "fulfilled") {
      const newObjs = listRes.value.data.objects || [];

      // Build merged list: list objects + any threats not already in the list
      const listIds = new Set(newObjs.map((o) => String(o.NORAD_CAT_ID)));
      const missingThreats = threats.filter(
        (t) => !listIds.has(String(t.NORAD_CAT_ID))
      );
      const merged = [...missingThreats, ...newObjs];

      if (reset || start === 0) {
        setDebrisObjects(merged);
        setDebrisStart(merged.length);
      } else {
        setDebrisObjects((prev) => {
          const prevIds = new Set(prev.map((o) => String(o.NORAD_CAT_ID)));
          const deduped = merged.filter(
            (o) => !prevIds.has(String(o.NORAD_CAT_ID))
          );
          return [...prev, ...deduped];
        });
        setDebrisStart((prev) => prev + newObjs.length);
      }
      setDebrisCount(listRes.value.data.total || 0);
      setTierCounts(listRes.value.data.tiers || null);
      setDebrisIsMock(!!listRes.value.data.is_mock_data);
    } else {
      console.error("Debris list fetch failed:", listRes.reason);
    }

    if (heatRes.status === "fulfilled") {
      setAltitudeBands(heatRes.value.data.bands || []);
    } else {
      console.error("Debris heatmap fetch failed:", heatRes.reason);
    }
  }, [debrisStart]);

  // The socket effect below runs once, so its handlers would otherwise capture
  // a stale fetchDebris. Always call through this ref.
  const fetchDebrisRef = useRef(fetchDebris);
  useEffect(() => { fetchDebrisRef.current = fetchDebris; }, [fetchDebris]);

  const loadMoreDebris = useCallback(async () => {
    try {
      const res = await axios.get(`${API}/debris/list?start=${debrisStart}&limit=${DEBRIS_PAGE_SIZE}&sort=risk`);
      const more = res.data.objects || [];
      if (more.length > 0) {
        setDebrisObjects((prev) => [...prev, ...more]);
        setDebrisStart(debrisStart + more.length);
      }
    } catch (e) {
      console.error("Failed to load more debris:", e);
    }
  }, [debrisStart]);

  const fetchISS = useCallback(async () => {
    try {
      const [posRes, proxRes] = await Promise.all([
        axios.get(`${API}/iss/position`),
        axios.get(`${API}/iss/proximity`),
      ]);
      setRawIssPosition(posRes.data);
      setIssPosition(posRes.data);
      setIssProximity(proxRes.data);
    } catch (e) {
      console.error("ISS fetch error:", e);
    }
  }, []);

  const fetchConjunctions = useCallback(async () => {
    try {
      const res = await axios.get(`${API}/conjunction/upcoming?hours=72&threshold=25&type=all`);
      setConjunctions(res.data.conjunctions || []);
      setLastScreened(res.data.last_screened || null);
      setConjunctionsScreening(!!res.data.screening);
    } catch (e) {
      console.error("Conjunction fetch error:", e);
    } finally {
      setConjunctionsLoading(false);
    }
  }, []);

  const fetchLatestSensor = useCallback(async () => {
    try {
      const res = await axios.get(`${API}/solar/latest-sensor`);
      if (res.data) {
        setLatestSensor(res.data);
        latestSensorRef.current = res.data;
        return res.data;
      }
    } catch (e) {
      console.error("Latest sensor fetch error:", e);
    }
    return null;
  }, []);

  // fetchSolar has zero deps — reads sensor via ref, never recreated.
  // Storm data from the sensor is ONLY merged when source is real hardware.
  // Mock/simulated sensor readings never influence the solar storm level.
  const fetchSolar = useCallback(async () => {
    const sensor = latestSensorRef.current;

    // Only trust storm data from a real ESP32/hardware sensor
    const sensorIsHardware = isHardwareSensor(sensor?.source);
    const sensorStormActive = sensorIsHardware ? (sensor?.solar?.storm_active ?? false) : false;
    const sensorKp          = sensorIsHardware ? (sensor?.solar?.kp_proxy ?? 0) : 0;
    const sensorLevel       = sensorIsHardware ? (sensor?.solar?.alert_level ?? null) : null;

    try {
      const sumRes = await axios.get(`${API}/solar/summary`);
      const donki  = sumRes.data || {};

      const donkiKp    = donki.max_kp_index ?? 0;
      const donkiLevel = donki.storm_level  ?? "NONE";

      // Merge: hardware sensor can raise the level above DONKI, but not mock sensor
      const mergedKp    = Math.max(donkiKp, sensorStormActive ? sensorKp : 0);
      const mergedLevel = donkiLevel !== "NONE"
        ? donkiLevel
        : sensorStormActive && sensorLevel
          ? sensorLevel
          : "NONE";

      const note = sensorStormActive && donkiLevel === "NONE"
        ? "Hardware sensor storm active (NOAA SWPC: no storm)"
        : donki.note || "Live NOAA SWPC data";

      setSolarSummary({
        ...donki,
        storm_level:  mergedLevel,
        max_kp_index: mergedKp,
        note,
      });

      console.log(
        `[AEGIS] Solar — NOAA SWPC: ${donkiLevel} kp=${donkiKp} | ` +
        `sensor(${sensor?.source ?? "none"}): ${sensorStormActive ? sensorLevel : "NONE"} kp=${sensorKp} | ` +
        `merged: ${mergedLevel} kp=${mergedKp} | ` +
        `flares=${donki.flare_count ?? 0} cme=${donki.cme_count ?? 0}`
      );
    } catch (e) {
      console.warn("Solar summary unavailable:", e.message);
      setSolarSummary((prev) => {
        if (prev) return prev;
        return {
          storm_level:    sensorStormActive ? sensorLevel || "MODERATE" : "NONE",
          max_kp_index:   sensorStormActive ? sensorKp : 0,
          flare_count:    0,
          x_class_flares: 0,
          m_class_flares: 0,
          cme_count:      0,
          latest_flares:  [],
          latest_cmes:    [],
          note: sensorStormActive
            ? "Hardware sensor storm active (NOAA SWPC unavailable)"
            : "NOAA SWPC unavailable",
        };
      });
    }

    try {
      const corrRes = await axios.get(`${API}/solar/correlation?hours=24`);
      setCorrelationSeries(corrRes.data.series || []);
    } catch (e) {
      console.error("Correlation fetch error:", e);
    }
  }, []); // stable — no deps

  // ─── WebSocket ─────────────────────────────────────────────────────────────
  useEffect(() => {
    const socket = io(SOCKET_URL, { transports: ["websocket"], path: "/socket.io" });
    socketRef.current = socket;

    socket.on("connect", () => {
      setConnected(true);
      console.log("[AEGIS] Socket connected");
      socket.emit("request_sensor_data");
    });
    socket.on("connect_error", (err) => console.warn("[AEGIS] Socket connect error", err));
    socket.on("error",         (err) => console.warn("[AEGIS] Socket error", err));
    socket.on("disconnect",    ()    => { setConnected(false); console.log("[AEGIS] Socket disconnected"); });

    socket.on("initial_state", (data) => {
      setDebrisCount(data.debris_count || 0);
      setCurrentRisk(data.risk_score   || 0);
      setSensorSource(normalizeSensorSource(data.sensor_source));
      setSensorDetail(data.sensor_detail || null);
      // (Re)connect — e.g. the backend was restarted while this page stayed open.
      // Re-pull the debris so we don't keep showing the previous run's objects.
      if ((data.debris_count || 0) > 0) fetchDebrisRef.current(true);
    });

    // The pipeline publishes real objects (stage "heuristic") and then GNN-refined
    // scores (stage "final"). Previously nothing told the page, so it only noticed
    // on its 10-minute poll and kept showing the startup data until then.
    socket.on("debris_updated", (data) => {
      setCurrentRisk(data.risk_score || 0);
      setDebrisCount(data.count || 0);
      fetchDebrisRef.current(true);
    });

    // Backend pushes this after each screening run with RED/AMBER events.
    socket.on("conjunction_alert", () => fetchConjunctions());
    // After EVERY run (also when nothing is RED/AMBER), so the list/count update
    // right away instead of on the next 60 s poll.
    socket.on("conjunctions_updated", () => fetchConjunctions());

    socket.on("sensor_reading", (data) => {
      console.log("[AEGIS] socket sensor_reading", data);
      const sensorUpdate = {
        ...data,
        mag:   data.mag   ? { ...data.mag }   : undefined,
        rf:    data.rf    ? { ...data.rf }    : undefined,
        solar: data.solar ? { ...data.solar } : undefined,
      };
      setLatestSensor(sensorUpdate);
      latestSensorRef.current = sensorUpdate;

      // RF-only readings (no magnetometer yet — e.g. USGS unreachable) update the
      // RF panel above but must NOT add a chart point: mag_total would default to 0
      // and draw a false drop to zero on the correlation line.
      if (sensorUpdate.mag?.total == null) return;

      setCorrelationSeries((prev) => {
        const point = {
          timestamp:    sensorUpdate.timestamp,
          mag_total:    sensorUpdate.mag?.total    ?? 0,
          rf_noise:     sensorUpdate.rf?.noise_dbm ?? -92,
          kp_proxy:     sensorUpdate.solar?.kp_proxy ?? 0,
          storm_active: sensorUpdate.solar?.storm_active ?? false,
          risk_score:   prev.length > 0 ? prev[prev.length - 1].risk_score : 0,
        };
        return [...prev, point].slice(-2880);
      });
    });

    // Only apply solar storm alerts from real hardware sensors.
    // Mock/simulated sensor storm events are silently ignored.
    socket.on("solar_storm_alert", (data) => {
      const source = data.source ?? data.sensor_source ?? "mock";
      if (!isHardwareSensor(source)) {
        console.log(`[AEGIS] Ignoring solar_storm_alert from non-hardware source: "${source}"`);
        return;
      }

      const normalized = {
        ...data,
        level: data.level || data.alert_level || "NONE",
        kp:    data.kp ?? data.kp_proxy ?? 0,
      };
      setSocketSolarAlert(normalized);

      setSolarSummary((prev) => ({
        ...(prev || {}),
        storm_level:  normalized.level || prev?.storm_level || "NONE",
        max_kp_index: Math.max(normalized.kp, prev?.max_kp_index ?? 0),
        note: "Hardware sensor storm active",
      }));

      setTimeout(() => {
        setSocketSolarAlert(null);
        fetchSolar();
      }, 30000);
    });

    return () => socket.disconnect();
  }, []);

  // ─── Initial load + polling ────────────────────────────────────────────────
  useEffect(() => {
    fetchDebris(true);
    fetchISS();
    fetchSolar();
    fetchConjunctions();

    const conjTimer   = setInterval(fetchConjunctions, 60 * 1000);
    const debrisTimer = setInterval(() => fetchDebris(true), 10 * 60 * 1000);
    const issTimer    = setInterval(fetchISS,   30 * 1000);
    const solarTimer  = setInterval(fetchSolar, 30 * 1000);

    return () => {
      clearInterval(conjTimer);
      clearInterval(debrisTimer);
      clearInterval(issTimer);
      clearInterval(solarTimer);
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Poll sensor independently when socket is disconnected
  useEffect(() => {
    if (connected) return;
    fetchLatestSensor();
    const t = setInterval(fetchLatestSensor, 1000);
    return () => clearInterval(t);
  }, [connected, fetchLatestSensor]);

  // ─── ISS 1-second interpolation ───────────────────────────────────────────
  useEffect(() => {
    if (!rawIssPosition) return;
    if (issInterpolRef.current) clearInterval(issInterpolRef.current);

    const ISS_SPEED_KM_S  = 7.66;
    const EARTH_RADIUS_KM = 6371;
    const INCLINATION_RAD = (51.6 * Math.PI) / 180;

    const normalizeTimestamp = (t) => {
      const v = Number(t);
      if (!Number.isFinite(v) || v <= 0) return Date.now() / 1000;
      return v > 1e12 ? v / 1000 : v;
    };

    const baseLat  = Number(rawIssPosition.lat ?? 0);
    const baseLon  = Number(rawIssPosition.lon ?? 0);
    const baseAlt  = Number(rawIssPosition.alt_km ?? rawIssPosition.alt ?? 408);
    const baseTime = rawIssPosition.timestamp
      ? normalizeTimestamp(rawIssPosition.timestamp)
      : Date.now() / 1000;

    function propagateISS(elapsed) {
      const distKm      = ISS_SPEED_KM_S * elapsed;
      const angularDist = distKm / EARTH_RADIUS_KM;
      const latRad      = (baseLat * Math.PI) / 180;
      const u0          = Math.asin(Math.sin(latRad) / Math.sin(INCLINATION_RAD));
      const u           = u0 + angularDist;

      const newLat = Math.max(-90, Math.min(90,
        Math.asin(Math.sin(INCLINATION_RAD) * Math.sin(u)) * (180 / Math.PI)
      ));

      const earthRotDeg = (360 / 86164) * elapsed;
      const deltaLon    = Math.atan2(Math.cos(INCLINATION_RAD) * Math.sin(u),  Math.cos(u))  * (180 / Math.PI);
      const baseDelta   = Math.atan2(Math.cos(INCLINATION_RAD) * Math.sin(u0), Math.cos(u0)) * (180 / Math.PI);
      const newLon      = ((baseLon + (deltaLon - baseDelta) - earthRotDeg + 180) % 360 + 360) % 360 - 180;

      return { lat: parseFloat(newLat.toFixed(4)), lon: parseFloat(newLon.toFixed(4)) };
    }

    const tick = () => {
      const elapsed = Math.max(0, Date.now() / 1000 - baseTime);
      const pos = propagateISS(elapsed);
      setIssPosition({ ...rawIssPosition, lat: pos.lat, lon: pos.lon, alt_km: baseAlt });
    };

    tick();
    issInterpolRef.current = setInterval(tick, 1000);
    return () => clearInterval(issInterpolRef.current);
  }, [rawIssPosition]);

  // ─── Derive solarAlert from solarSummary ──────────────────────────────────
  // Hardware socket alert takes priority. When no hardware alert is active,
  // derive a persistent alert from the REST summary (real NOAA SWPC data only).
  const solarAlert = (() => {
    if (socketSolarAlert) return socketSolarAlert;
    if (!solarSummary) return null;
    const level = solarSummary.storm_level;
    if (!level || level === "NONE") return null;
    return {
      level,
      kp:     solarSummary.max_kp_index ?? 0,
      source: "noaa",
    };
  })();

  return {
    connected,
    sensorSource,
    sensorDetail,
    conjunctionsScreening,
    debrisObjects,
    topThreats,
    altitudeBands,
    debrisCount,
    tierCounts,
    debrisIsMock,
    issPosition,
    issProximity,
    solarSummary,
    latestSensor,
    correlationSeries,
    solarAlert,
    currentRisk,
    conjunctions,
    lastScreened,
    conjunctionsLoading,
    refresh: { fetchDebris, fetchISS, fetchSolar },
    loadMoreDebris,
  };
}