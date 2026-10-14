# Overmortal Automation: Multi-Node Computer Vision Pipeline

A fault-tolerant Python automation engine that uses computer-vision template matching to launch, verify, and operate the Overmortal Android game running inside a BlueStacks emulator. It replaces brittle coordinate-based macro scripts with template-based navigation, dynamic re-scanning, and recovery from failure states.

## Architecture Overview
1. **Trigger Layer:** Designed to run on demand via Windows Task Scheduler, so it only executes at configured times and does not stay resident between runs.
2. **Control Layer:** Uses the Android Debug Bridge (ADB) to inject tap and swipe events into the emulated device, without relying on target window focus.
3. **Vision Layer:** Uses OpenCV normalized cross-correlation for template matching: a multi-scale grayscale search (9 scales) for per-frame scans, a Canny edge-detection fallback, and full-color matching to tell apart visually similar icons.
4. **State Machine Layer:** Runs failover chains when a target state is not found (live scan → stitched mosaic → calibrated re-navigation), and always runs cleanup on exit.

## Tech Stack
* **Language:** Python 3.10+
* **Core Libraries:** OpenCV (`opencv-python`), NumPy, DXcam (desktop capture), pywin32 (window enumeration)
* **Control Protocol:** Android Debug Bridge (ADB) via subprocess

## Requirements
* Python 3.10+
* Android SDK Platform-Tools (`adb.exe`), included in `Overmortal_Automation/platform-tools/`
* BlueStacks (`HD-Player.exe`) with the emulator set to a 1080x1920 portrait resolution
* Windows (DXcam and pywin32 are Windows-only)

## Usage
The script declares its dependencies inline (PEP 723), so [uv](https://docs.astral.sh/uv/) can run it directly:

```bash
uv run Overmortal_Automation/script.py
```

Each run appends a JSON line with phase timings and counters to `Overmortal_Automation/run_results.jsonl`.

## Highlights
* **Vision-Based Asset Detection:** Locates UI assets with multi-scale template matching, a Canny edge-detection fallback, and grayscale/color matching, so detection holds up under varying rendering conditions.
* **Map Navigation:** Replaces hardcoded tap coordinates with a boustrophedon (zig-zag) grid scan that captures 24 map cells (4 rows × 6 steps) and stitches them into a mosaic. Matches are mapped back to screen coordinates using the recorded camera-pan trajectories.
* **Failure Handling:** Multi-tier failover with duplicate-frame detection and ambiguous-match detection. Low-confidence matches are reported rather than tapped, and `try/finally` cleanup returns the system to a clean state even when a run fails.
* **Run Metrics:** Per-run phase timings and counters are saved to JSONL for aggregating success rates and failover-tier usage across runs.
