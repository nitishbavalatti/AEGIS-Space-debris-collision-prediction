#!/usr/bin/env python3
"""
RTL-SDR Diagnostic Tool
Checks if your RTL-SDR device is properly connected and recognized.
"""

import sys
import logging

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

def check_rtlsdr():
    """Run diagnostic checks on RTL-SDR setup"""
    
    print("=" * 70)
    print("RTL-SDR DIAGNOSTIC TOOL")
    print("=" * 70)
    
    # ─── Check 1: Can we import rtlsdr? ────────────────────────────────────
    print("\n[1] Checking if pyrtlsdr is installed...")
    try:
        from rtlsdr import RtlSdr
        print("    ✅ pyrtlsdr is installed")
    except ImportError as e:
        print(f"    ❌ pyrtlsdr not found: {e}")
        print("    → Install with: pip install pyrtlsdr")
        return False
    
    # ─── Check 2: Can libusb find any RTL-SDR devices? ────────────────────
    print("\n[2] Scanning for RTL-SDR devices...")
    try:
        # Try to enumerate devices using rtlsdr
        try:
            sdr = RtlSdr()
            print(f"    ✅ RTL-SDR device found and accessible!")
            print(f"    → Device index: 0")
            
            # Try to read sample count
            try:
                gain_values = sdr.get_gains()
                print(f"    → Available gains: {gain_values}")
            except:
                pass
            
            sdr.close()
            return True
            
        except FileNotFoundError as e:
            print(f"    ❌ No RTL-SDR devices found: {e}")
            return False
        except PermissionError as e:
            print(f"    ❌ Permission denied: {e}")
            print("    → Another app may be using the device (SDR#, GQRX, etc.)")
            return False
        except Exception as e:
            print(f"    ❌ Error: {e}")
            return False
            
    except Exception as e:
        print(f"    ❌ Unexpected error: {e}")
        return False

def print_solutions():
    """Print troubleshooting steps"""
    print("\n" + "=" * 70)
    print("TROUBLESHOOTING")
    print("=" * 70)
    
    print("""
If device not found, follow these steps:

1. PHYSICAL CONNECTION
   ✓ Plug RTL-SDR dongle into USB port
   ✓ Use USB 3.0 ports if available (better power)
   ✓ Try different USB port

2. WINDOWS DRIVERS (Required on Windows)
   ✓ Download Zadig: https://zadig.akeo.ie/
   ✓ Run Zadig and select your RTL-SDR device
   ✓ Install LibUSB 1.0 driver
   ✓ Device should appear as "RTL-SDR" in Zadig after install

3. CONFLICTING APPLICATIONS
   ✓ Close SDR# (HDSDR)
   ✓ Close GQRX
   ✓ Close CubicSDR
   ✓ Only one app can use RTL-SDR at a time

4. PYTHON PACKAGE
   ✓ pip install --upgrade pyrtlsdr

5. RESTART AFTER DRIVER INSTALL
   ✓ Restart Windows after install LibUSB drivers via Zadig
   ✓ Then restart this app
""")

if __name__ == "__main__":
    print("\nScanning RTL-SDR hardware...")
    device_found = check_rtlsdr()
    
    if device_found:
        print("\n" + "=" * 70)
        print("✅ SUCCESS: RTL-SDR is properly configured!")
        print("=" * 70)
        print("\nYour device should work with AEGIS. Run:")
        print("  python -m backend.app")
    else:
        print_solutions()
        sys.exit(1)
