# AEGIS — Space Debris Threat Monitor

> Real-time orbital debris tracking, GNN-powered collision risk prediction, and solar weather correlation dashboard.

## Architecture

```
┌─────────────────┐     WebSocket      ┌─────────────────┐
│  React Frontend │◄──────────────────►│  Flask Backend  │
│  (Vite + Cesium)│                    │ (Flask+SocketIO)│
└─────────────────┘                    └────────┬────────┘
                                                │
                    ┌───────────────────────────┼───────────────────────────┐
                    ▼                           ▼                           ▼
            ┌───────────────┐         ┌───────────────┐           ┌───────────────┐
            │ Space-Track   │         │ NOAA SWPC     │           │ N2YO API      │
            │ (TLE data)    │         │ (Solar storm) │           │ (ISS position)│
            └───────────────┘         └───────────────┘           └───────────────┘
```

## Features

- **3D Globe** — CesiumJS visualization with 500+ debris objects color-coded by risk level
- **GNN Risk Scoring** — Graph Attention Network predicts cascade collision probability
- **ISS Protection** — Real-time proximity alerts (RED <5km, AMBER <10km, GREEN)
- **Solar Weather** — NOAA SWPC integration for geomagnetic storm and Kp index status
- **Hardware Sensors** — ESP32 + HMC5883L magnetometer + RTL-SDR via MQTT
- **Kessler Simulator** — Canvas-based cascade collision visualization

## Quick Start

### Prerequisites
- Python 3.12+
- Node.js 20+
- (Optional) MongoDB, Mosquitto MQTT broker

### Backend

```bash
cd backend
cp .env.example .env   # edit with your API keys
pip install -r requirements.txt
python app.py
```

Backend runs at `http://localhost:5000`

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Frontend runs at `http://localhost:5173`

### Docker (all services)

```bash
docker compose up --build
```

This starts backend (5000), frontend (80), MongoDB (27017), and Mosquitto (1883).

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/health` | System health check |
| `GET /api/debris/list` | All debris objects with risk scores |
| `GET /api/debris/top-threats` | Top N highest-risk objects |
| `GET /api/debris/heatmap` | Altitude band density data |
| `GET /api/iss/position` | Live ISS position |
| `GET /api/iss/proximity` | Nearest debris to ISS |
| `GET /api/solar/summary` | Solar weather summary |
| `GET /api/solar/correlation` | Sensor ↔ GNN correlation time-series |

## Configuration

Copy `backend/.env.example` to `backend/.env` and fill in:

| Variable | Source | Required |
|----------|--------|----------|
| `VITE_CESIUM_TOKEN` | [Cesium Ion](https://ion.cesium.com) | Yes |
| `SPACETRACK_USERNAME` | [Space-Track](https://www.space-track.org) | No (falls back to mock) |
| `SPACETRACK_PASSWORD` | Space-Track | No |
| `N2YO_API_KEY` | [N2YO](https://www.n2yo.com/api/) | No (falls back to mock) |
| `NASA_API_KEY` | [NASA](https://api.nasa.gov) | No (DEMO_KEY works) |
| `NOAA_SWPC_URL` | [NOAA SWPC](https://services.swpc.noaa.gov) | No |
| `USE_MOCK_SENSOR` | — | No (default: true) |

## Hardware Setup

When ready to use real sensors:

1. Flash ESP32 with `esp32_aegis/` Arduino code (HMC5883L magnetometer)
2. Connect RTL-SDR dongle and run `python backend/rtlsdr_monitor.py`
3. Start Mosquitto: `sudo systemctl start mosquitto`
4. Set `USE_MOCK_SENSOR=false` in `.env`
5. Restart backend

## Training the GNN

```bash
cd backend
python -m backend.gnn.train
```

Model checkpoint saves to `gnn/aegis_gnn.pt`.

## Project Structure

```
AGEIS/
├── backend/              # Flask API + GNN + sensor
│   ├── app.py            # Main entry point
│   ├── config.py         # Environment configuration
│   ├── tasks.py          # Background data pipelines
│   ├── mock_sensor.py    # Simulated sensor (default mode)
│   ├── rtlsdr_monitor.py # RTL-SDR RF monitor
│   ├── gnn/              # Graph Neural Network
│   │   ├── model.py      # GAT architecture
│   │   └── train.py      # Self-supervised training
│   ├── data/             # SGP4 propagation, proximity
│   ├── routes/           # REST API blueprints
│   ├── services/         # External API clients
│   └── tests/            # pytest suite
├── frontend/             # React dashboard
│   ├── src/
│   │   ├── App.jsx       # Main layout (3-column grid)
│   │   ├── hooks/useAegis.js  # Central data hook
│   │   └── components/   # Dashboard panels
├── esp32_aegis/          # Arduino firmware (TBD)
├── docker-compose.yml    # Full stack orchestration
└── .gitignore
```

## License

MIT
