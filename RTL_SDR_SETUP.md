# RTL-SDR Hardware Setup Guide

## Current Issue: Constant -92 dBm Readings

Your AEGIS app is showing **constant -92.0 dBm** readings. This is the **placeholder value** indicating the RTL-SDR device is **not actually connected**.

**Expected normal readings:** -95 to -90 dBm with variation  
**Expected storm readings:** -60 to -80 dBm spikes  
**Your readings:** Always exactly -92 dBm (placeholder)

---

## Quick Fix

### Step 1: Run the Diagnostic
```bash
python rtlsdr_diagnostic.py
```

This will tell you exactly what's wrong.

### Step 2: Check Hardware
- [ ] RTL-SDR dongle is plugged into USB port
- [ ] No other SDR app is running (SDR#, GQRX, CubicSDR)

### Step 3: Install Windows Drivers (CRITICAL on Windows)
1. Download **Zadig** from https://zadig.akeo.ie/
2. Run Zadig
3. Select your RTL-SDR device from the dropdown
4. Select **LibUSB-1.0** from the driver dropdown
5. Click **Install Driver**
6. **Restart Windows** (important!)
7. Restart AEGIS

---

## Temporary Workaround

Until your RTL-SDR is set up, use the **MockSensor** which generates realistic fake RF data:

Edit `.env`:
```
USE_MOCK_SENSOR=true
```

Then restart the app. It will generate realistic solar storm events and RF variations for testing.

---

## What the App Logs Tell You

When you start the app, check the logs:

### ✅ Device Working
```
[HW Sensor] ✅ RTL-SDR initialized successfully - device is connected
[HW Sensor] ✅ RTL-SDR thread started - reading RF data from device
[RTL-SDR] noise=-91.42dBm delta=3.42dBFS alert=NONE
```

### ❌ Device Not Found
```
[HW Sensor] ❌ RTL-SDR device NOT FOUND
[HW Sensor] → Ensure RTL-SDR dongle is plugged into USB
[HW Sensor] ⚠️  RTL-SDR DISABLED - will use MQTT/ESP32 data only
```

### ❌ Permission Denied
```
[HW Sensor] ❌ RTL-SDR permission denied
[HW Sensor] → On Windows, ensure no other app (SDR#, GQRX) is using the device
```

---

## Hardware Requirements

- **RTL-SDR Dongle** (RTL-SDR v3, RTL-SDR Blog v4, or similar)
- **USB Port** (USB 3.0 preferred for power)
- **Windows Drivers** via Zadig (see Step 3 above)
- **Python 3.9+** with `python-rtlsdr` installed

---

## Reference: Normal RF Readings

From NOAA band (137.5 MHz) in Bengaluru:

| Condition | Noise Level | Delta | Alert |
|-----------|------------|-------|--------|
| Quiet ionosphere | -92 to -90 dBm | -4 to -2 dBFS | NONE |
| Minor activity | -88 to -86 dBm | +0 to +2 dBFS | NONE |
| Solar storm | -75 to -60 dBm | +13 to +28 dBFS | RF_SPIKE |

---

## Still Having Issues?

Check:
1. Plug device into **different USB port**
2. Try **USB 2.0 port** if USB 3.0 fails
3. Close **all other SDR applications**
4. Check Windows Device Manager for unknown devices
5. Run `python rtlsdr_diagnostic.py` and share output
