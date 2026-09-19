# Charging

A lightweight macOS battery dashboard. Python's standard library, plain HTML,
CSS, and JavaScript only. No packages, cloud services, or persisted telemetry.

## Browser dashboard

From any terminal directory:

```sh
charging
```

The installed launcher at `~/.local/bin/charging` points to this project and
opens your default browser. Keep the terminal command running while using the
dashboard. Press **Ctrl+C** to stop the server. Closing or hiding the tab stops
its periodic requests; the idle server remains running until you stop it.

```sh
charging --no-open       # Print the local URL without opening a tab
charging --port 8765     # Use a specific port instead of a free random port
```

Without the installed launcher: `python3 charging_web.py`.
The server listens only on `127.0.0.1`. Open the exact URL printed in the terminal.
Move the project or Python installation only after updating the launcher.

Readings refresh every three seconds while the page is visible. The chart keeps
two minutes of signed battery power in tab memory. Positive power charges the
battery; negative power leaves it. Adapter input and charger rating are separate
measurements, not interchangeable with battery power. Missing telemetry is
shown as a dash. Availability and accuracy depend on the Mac's reported sensors.

The power distribution bar shows battery versus Mac + conversion losses as a
percentage of actual adapter input, not its rated capacity. Mac power is an
estimate: adapter input minus signed battery power (or battery discharge when
unplugged). Inconsistent readings hide the split rather than invent percentages.
Cell balance shows reported cell voltages and their maximum spread.

Temperature history covers ten minutes. The charging curve averages charging
watts at each percentage level observed while the page is visible; it resets
when a new plug-in is observed or the page reloads. Gaps in observed charge levels
are not connected. Both charts live only in tab memory; hidden-tab periods are
not sampled and no historical data is saved.

## Original terminal interface

```sh
python3 charging_monitor.py
```

Press **q** to quit.
