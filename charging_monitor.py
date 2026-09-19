#!/usr/bin/env python3
"""
⚡ Charging Monitor — Real-time battery & charger stats for macOS
Similar to "What Watt?" / "Watts Connected" but in your terminal.

Usage: python3 charging_monitor.py
"""

import curses
import subprocess
import re
import time
import sys
from collections import deque
from datetime import datetime


# ── Data Fetching ──────────────────────────────────────────────────────────────

def get_ioreg_battery():
    """Parse AppleSmartBattery from ioreg into a dict."""
    try:
        raw = subprocess.check_output(
            ["ioreg", "-rn", "AppleSmartBattery"], text=True, timeout=5
        )
    except Exception:
        return {}

    data = {}

    # Simple key-value pairs
    simple_keys = [
        "CurrentCapacity", "MaxCapacity", "DesignCapacity", "CycleCount",
        "Voltage", "Amperage", "InstantAmperage", "Temperature",
        "IsCharging", "FullyCharged", "ExternalConnected",
        "TimeRemaining", "AvgTimeToFull", "AvgTimeToEmpty",
        "AppleRawCurrentCapacity", "AppleRawMaxCapacity",
        "AppleRawBatteryVoltage", "NominalChargeCapacity",
    ]
    # Match top-level keys only (indented with exactly 6 spaces in ioreg output)
    for key in simple_keys:
        m = re.search(rf'^\s+"' + key + r'"\s*=\s*(\S+)', raw, re.MULTILINE)
        if m:
            val = m.group(1).strip()
            if val == "Yes":
                data[key] = True
            elif val == "No":
                data[key] = False
            else:
                try:
                    data[key] = int(val)
                except ValueError:
                    data[key] = val

    # Adapter details — search across full raw output for keys
    # (avoids issues with nested braces in AdapterDetails)
    adapter = {}
    # Only parse if AdapterDetails exists
    if '"AdapterDetails"' in raw:
        for k in ["Name", "Manufacturer", "Description", "SerialString",
                   "Watts", "AdapterVoltage", "Current", "AdapterID",
                   "FwVersion", "HwVersion", "Model", "IsWireless"]:
            # Match within AdapterDetails context
            m = re.search(rf'"AdapterDetails".*?"{k}"\s*=\s*"?([^",\}}]+)"?', raw, re.DOTALL)
            if m:
                val = m.group(1).strip()
                try:
                    adapter[k] = int(val)
                except ValueError:
                    if val == "Yes":
                        adapter[k] = True
                    elif val == "No":
                        adapter[k] = False
                    else:
                        adapter[k] = val
        # Parse USB PD voltage menu from UsbHvcMenu
        hvc_matches = re.findall(
            r'"MaxCurrent"\s*=\s*(\d+)\s*,\s*"MaxVoltage"\s*=\s*(\d+)', raw
        )
        if hvc_matches:
            # Deduplicate (appears in both Raw and regular AdapterDetails)
            seen = set()
            profiles = []
            for a, v in hvc_matches:
                key = (int(v), int(a))
                if key not in seen:
                    seen.add(key)
                    profiles.append((int(v) / 1000, int(a) / 1000))
            adapter["PD_Profiles"] = profiles
        data["Adapter"] = adapter

    # Charger data
    charger_match = re.search(r'"ChargerData"\s*=\s*\{([^}]+)\}', raw)
    if charger_match:
        block = charger_match.group(1)
        charger = {}
        for k in ["ChargingVoltage", "ChargingCurrent", "NotChargingReason",
                   "SlowChargingReason", "ChargerID"]:
            m = re.search(rf'"{k}"\s*=\s*(\d+)', block)
            if m:
                charger[k] = int(m.group(1))
        data["Charger"] = charger

    # Power telemetry
    pwr_match = re.search(r'"PowerTelemetryData"\s*=\s*\{([^}]+)\}', raw)
    if pwr_match:
        block = pwr_match.group(1)
        pwr = {}
        for k in ["SystemPowerIn", "BatteryPower", "WallEnergyEstimate",
                   "SystemCurrentIn", "SystemVoltageIn", "SystemLoad"]:
            m = re.search(rf'"{k}"\s*=\s*(\d+)', block)
            if m:
                pwr[k] = int(m.group(1))
        data["PowerTelemetry"] = pwr

    return data


# ── Drawing Helpers ────────────────────────────────────────────────────────────

BLOCK_CHARS = " ▏▎▍▌▋▊▉█"

def draw_bar(win, y, x, width, fraction, color_pair):
    """Draw a smooth progress bar using Unicode block chars."""
    fraction = max(0.0, min(1.0, fraction))
    full_blocks = int(fraction * width)
    remainder = (fraction * width) - full_blocks
    partial_idx = int(remainder * 8)

    bar = "█" * full_blocks
    if full_blocks < width:
        bar += BLOCK_CHARS[partial_idx]
        bar += " " * (width - full_blocks - 1)

    try:
        win.addstr(y, x, bar[:width], curses.color_pair(color_pair))
    except curses.error:
        pass


def draw_spark_line(win, y, x, width, values, max_val, color_pair):
    """Draw a sparkline chart of historical values."""
    spark_chars = "▁▂▃▄▅▆▇█"
    if not values or max_val <= 0:
        return
    n = min(width, len(values))
    recent = list(values)[-n:]
    line = ""
    for v in recent:
        idx = int((v / max_val) * 7)
        idx = max(0, min(7, idx))
        line += spark_chars[idx]
    # pad left
    line = " " * (width - len(line)) + line
    try:
        win.addstr(y, x, line[:width], curses.color_pair(color_pair))
    except curses.error:
        pass


BIG_DIGITS = {
    '0': ["╔═╗", "║ ║", "╚═╝"],
    '1': ["  ╗", "  ║", "  ╝"],
    '2': ["╔═╗", "╔═╝", "╚═╝"],
    '3': ["╔═╗", " ═╣", "╚═╝"],
    '4': ["╗ ╗", "╚═╣", "  ╝"],
    '5': ["╔═╗", "╚═╗", "╚═╝"],
    '6': ["╔═╗", "╠═╗", "╚═╝"],
    '7': ["╔═╗", "  ║", "  ╝"],
    '8': ["╔═╗", "╠═╣", "╚═╝"],
    '9': ["╔═╗", "╚═╣", "╚═╝"],
    '.': ["   ", "   ", " ● "],
    ' ': ["   ", "   ", "   "],
    'W': ["     ", "     ", " W   "],
}


def draw_big_number(win, y, x, text, color_pair):
    """Draw a large 3-row number display."""
    for row in range(3):
        col = x
        for ch in text:
            glyph = BIG_DIGITS.get(ch, BIG_DIGITS[' '])
            try:
                win.addstr(y + row, col, glyph[row], curses.color_pair(color_pair))
            except curses.error:
                pass
            col += len(glyph[row]) + 1


def safe_addstr(win, y, x, text, *args):
    """addstr that silently ignores out-of-bounds."""
    try:
        win.addstr(y, x, text, *args)
    except curses.error:
        pass


# ── Main TUI ───────────────────────────────────────────────────────────────────

def main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(1000)  # refresh every 1s

    # Colors
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)    # charging / good
    curses.init_pair(2, curses.COLOR_YELLOW, -1)   # warning
    curses.init_pair(3, curses.COLOR_RED, -1)       # critical
    curses.init_pair(4, curses.COLOR_CYAN, -1)      # info / headers
    curses.init_pair(5, curses.COLOR_MAGENTA, -1)   # accent
    curses.init_pair(6, curses.COLOR_WHITE, -1)     # normal
    curses.init_pair(7, curses.COLOR_GREEN, curses.COLOR_BLACK)  # bar fill
    curses.init_pair(8, curses.COLOR_BLUE, -1)      # dim info

    HISTORY_LEN = 120  # 2 minutes at 1s intervals
    watt_history = deque(maxlen=HISTORY_LEN)
    amp_history = deque(maxlen=HISTORY_LEN)
    volt_history = deque(maxlen=HISTORY_LEN)
    pct_history = deque(maxlen=HISTORY_LEN)
    timestamps = deque(maxlen=HISTORY_LEN)

    sample_count = 0
    start_time = time.time()
    start_pct = None

    while True:
        key = stdscr.getch()
        if key in (ord('q'), ord('Q'), 27):  # q or ESC
            break

        data = get_ioreg_battery()
        if not data:
            stdscr.clear()
            safe_addstr(stdscr, 1, 2, "⚠  Could not read battery data", curses.color_pair(3))
            stdscr.refresh()
            continue

        now = time.time()
        sample_count += 1

        # ── Extract values ──
        pct = data.get("CurrentCapacity", 0)
        is_charging = data.get("IsCharging", False)
        fully_charged = data.get("FullyCharged", False)
        external = data.get("ExternalConnected", False)

        # Battery voltage in V, amperage in mA
        batt_voltage_mv = data.get("AppleRawBatteryVoltage", data.get("Voltage", 0))
        batt_voltage = batt_voltage_mv / 1000.0
        amperage_ma = data.get("InstantAmperage", data.get("Amperage", 0))
        amperage_a = amperage_ma / 1000.0

        # Wattage into battery
        wattage = batt_voltage * abs(amperage_a)

        # System-level power (from adapter)
        pwr = data.get("PowerTelemetry", {})
        system_power_mw = pwr.get("SystemPowerIn", 0)
        system_power = system_power_mw / 1000.0 if system_power_mw else 0
        system_voltage_mv = pwr.get("SystemVoltageIn", 0)
        system_current_ma = pwr.get("SystemCurrentIn", 0)

        # Adapter info
        adapter = data.get("Adapter", {})
        adapter_name = adapter.get("Name", "Unknown")
        adapter_watts = adapter.get("Watts", 0)
        adapter_mfr = adapter.get("Manufacturer", "")
        adapter_voltage_mv = adapter.get("AdapterVoltage", 0)
        adapter_current_ma = adapter.get("Current", 0)
        pd_profiles = adapter.get("PD_Profiles", [])

        # Charger controller
        charger = data.get("Charger", {})
        charge_voltage_mv = charger.get("ChargingVoltage", 0)
        charge_current_ma = charger.get("ChargingCurrent", 0)

        # Time
        time_to_full = data.get("AvgTimeToFull", 65535)
        time_to_empty = data.get("AvgTimeToEmpty", 65535)
        temp_raw = data.get("Temperature", 0)
        temp_c = temp_raw / 100.0
        temp_f = temp_c * 9 / 5 + 32
        cycle_count = data.get("CycleCount", 0)
        design_cap = data.get("DesignCapacity", 0)
        raw_max_cap = data.get("AppleRawMaxCapacity", 0)
        raw_cur_cap = data.get("AppleRawCurrentCapacity", 0)
        health_pct = (raw_max_cap / design_cap * 100) if design_cap else 0

        # Track history
        if is_charging:
            watt_history.append(wattage)
        elif external and not is_charging:
            watt_history.append(0)
        else:
            watt_history.append(-wattage)  # discharging shown as negative
        amp_history.append(abs(amperage_a))
        volt_history.append(batt_voltage)
        pct_history.append(pct)
        timestamps.append(now)

        if start_pct is None:
            start_pct = pct

        # ── Draw ──
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        if h < 20 or w < 60:
            safe_addstr(stdscr, 0, 0, "Terminal too small (need 60x20+)", curses.color_pair(3))
            stdscr.refresh()
            continue

        col2 = max(40, w // 2)
        row = 0

        # ── Header ──
        title = "⚡ CHARGING MONITOR"
        safe_addstr(stdscr, row, 2, title, curses.color_pair(4) | curses.A_BOLD)
        ts = datetime.now().strftime("%H:%M:%S")
        safe_addstr(stdscr, row, w - len(ts) - 2, ts, curses.color_pair(8))
        row += 1
        safe_addstr(stdscr, row, 2, "─" * (w - 4), curses.color_pair(8))
        row += 1

        # ── Big wattage display ──
        if is_charging:
            watt_str = f"{wattage:.1f}"
            watt_color = 1  # green
            watt_label = "WATTS INTO BATTERY"
        elif external:
            watt_str = f"{system_power:.1f}" if system_power else "0.0"
            watt_color = 2  # yellow
            watt_label = "WATTS (MAINTAINING)"
        else:
            watt_str = f"{wattage:.1f}"
            watt_color = 3  # red
            watt_label = "WATTS DISCHARGING"

        draw_big_number(stdscr, row, 3, watt_str, watt_color)
        # "W" label next to big number
        big_w_x = 3 + (len(watt_str)) * 4 + 1
        safe_addstr(stdscr, row + 1, big_w_x, "W", curses.color_pair(watt_color) | curses.A_BOLD)
        safe_addstr(stdscr, row + 2, big_w_x + 3, watt_label, curses.color_pair(8))
        row += 4

        # ── Adapter / Charger Info ──
        safe_addstr(stdscr, row, 2, "CHARGER", curses.color_pair(4) | curses.A_BOLD)
        row += 1
        if external and adapter_name != "Unknown":
            safe_addstr(stdscr, row, 4, f"📛 {adapter_name}", curses.color_pair(6))
            if adapter_mfr:
                mfr_x = 6 + len(adapter_name) + 2
                safe_addstr(stdscr, row, mfr_x, f"({adapter_mfr})", curses.color_pair(8))
            row += 1
            safe_addstr(stdscr, row, 4, f"⚡ Rated: {adapter_watts}W", curses.color_pair(5))
            neg_v = adapter_voltage_mv / 1000
            neg_a = adapter_current_ma / 1000
            safe_addstr(stdscr, row, 24,
                        f"│ Negotiated: {neg_v:.1f}V × {neg_a:.2f}A = {neg_v * neg_a:.1f}W",
                        curses.color_pair(6))
            row += 1

            # USB PD profiles
            if pd_profiles:
                pd_str = "PD: " + " / ".join(f"{v:.0f}V@{a:.1f}A" for v, a in pd_profiles)
                safe_addstr(stdscr, row, 4, pd_str, curses.color_pair(8))
                row += 1
        elif external:
            safe_addstr(stdscr, row, 4, "Connected (details unavailable)", curses.color_pair(2))
            row += 1
        else:
            safe_addstr(stdscr, row, 4, "❌ Not connected", curses.color_pair(3))
            row += 1

        row += 1

        # ── Battery section ──
        safe_addstr(stdscr, row, 2, "BATTERY", curses.color_pair(4) | curses.A_BOLD)
        row += 1

        # Battery bar
        bar_w = min(40, w - 20)
        pct_color = 1 if pct > 50 else (2 if pct > 20 else 3)
        safe_addstr(stdscr, row, 4, "│", curses.color_pair(6))
        draw_bar(stdscr, row, 5, bar_w, pct / 100.0, pct_color)
        safe_addstr(stdscr, row, 5 + bar_w, "│", curses.color_pair(6))
        status_icon = "🔋" if pct > 20 else "🪫"
        status_text = "⚡" if is_charging else ("✓" if fully_charged else "")
        safe_addstr(stdscr, row, 7 + bar_w, f"{status_icon} {pct}% {status_text}", curses.color_pair(pct_color) | curses.A_BOLD)
        row += 1

        # Stats in two columns
        safe_addstr(stdscr, row, 4,
                    f"Voltage:  {batt_voltage:.3f} V", curses.color_pair(6))
        safe_addstr(stdscr, row, col2,
                    f"Current:  {abs(amperage_ma)} mA", curses.color_pair(6))
        row += 1

        safe_addstr(stdscr, row, 4,
                    f"Temp:     {temp_c:.1f}°C / {temp_f:.1f}°F", curses.color_pair(6))
        if is_charging and time_to_full < 65535:
            hrs = time_to_full // 60
            mins = time_to_full % 60
            safe_addstr(stdscr, row, col2,
                        f"Full in:  {hrs}h {mins:02d}m", curses.color_pair(1))
        elif not external and time_to_empty < 65535:
            hrs = time_to_empty // 60
            mins = time_to_empty % 60
            safe_addstr(stdscr, row, col2,
                        f"Empty in: {hrs}h {mins:02d}m", curses.color_pair(3))
        row += 1

        safe_addstr(stdscr, row, 4,
                    f"Health:   {health_pct:.1f}%  ({raw_max_cap}/{design_cap} mAh)",
                    curses.color_pair(6))
        safe_addstr(stdscr, row, col2,
                    f"Cycles:   {cycle_count}", curses.color_pair(6))
        row += 1

        if system_power > 0:
            safe_addstr(stdscr, row, 4,
                        f"System:   {system_power:.1f}W from adapter",
                        curses.color_pair(8))
            if is_charging and system_power > 0:
                efficiency = (wattage / system_power * 100) if system_power else 0
                safe_addstr(stdscr, row, col2,
                            f"To batt:  {efficiency:.0f}% of input",
                            curses.color_pair(8))
            row += 1

        # Charging rate (% per hour)
        elapsed = now - start_time
        if elapsed > 10 and len(pct_history) > 1:
            pct_delta = pct - start_pct
            rate = pct_delta / (elapsed / 3600)
            if abs(rate) > 0.01:
                rate_str = f"Rate:     {rate:+.1f}%/hr"
                rate_color = 1 if rate > 0 else 3
                safe_addstr(stdscr, row, 4, rate_str, curses.color_pair(rate_color))
                row += 1

        row += 1

        # ── Power History Chart ──
        chart_h_avail = h - row - 3
        if chart_h_avail >= 3 and len(watt_history) > 1:
            safe_addstr(stdscr, row, 2, "POWER HISTORY", curses.color_pair(4) | curses.A_BOLD)
            chart_w = min(w - 8, HISTORY_LEN)
            max_w = max(max(abs(v) for v in watt_history), 1)

            # Draw scale
            safe_addstr(stdscr, row, 18, f"(max {max_w:.1f}W, {len(watt_history)}s window)",
                        curses.color_pair(8))
            row += 1

            draw_spark_line(stdscr, row, 4, chart_w,
                            [abs(v) for v in watt_history], max_w, 1)
            row += 1
            safe_addstr(stdscr, row, 4, f"0W", curses.color_pair(8))
            scale_end = f"{max_w:.0f}W"
            safe_addstr(stdscr, row, 4 + chart_w - len(scale_end), scale_end,
                        curses.color_pair(8))
            row += 1

        # ── Footer ──
        footer = " q: quit │ updates every 1s "
        safe_addstr(stdscr, h - 1, 2, footer, curses.color_pair(8))

        stdscr.refresh()


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    print("Charging monitor stopped.")
