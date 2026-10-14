# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "opencv-python",
#     "numpy",
#     "dxcam",
#     "pywin32",
# ]
# ///
import json
import logging
import os
import re
import subprocess
import time

import cv2
import numpy as np

# =====================================================================
# SYSTEM INITIALIZATION & LOGGING
# =====================================================================

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

BLUESTACKS_EXE_PATH = r"C:\Program Files\BlueStacks_nxt\HD-Player.exe"
OVERMORTAL_PACKAGE_NAME = "com.ltgames.android.overmortal"


class RunStats:
    """Tiny metrics collector - counts events, times phases, dumps a report.

    Every run appends one JSON line to ``run_results.jsonl`` so success rates,
    average cycle times, and failover-tier usage can be aggregated across runs
    into real, resume-defensible numbers.
    """

    def __init__(self):
        self.phase_times = {}       # phase name -> elapsed seconds (float)
        self.counters = {}          # metric name -> int
        self._phase_start = {}      # phase name -> time.monotonic() start

    def start_phase(self, name):
        self._phase_start[name] = time.monotonic()

    def end_phase(self, name):
        start = self._phase_start.pop(name, None)
        if start is not None:
            self.phase_times[name] = round(time.monotonic() - start, 1)

    def inc(self, key, n=1):
        self.counters[key] = self.counters.get(key, 0) + n

    def as_dict(self):
        return {"phase_times": self.phase_times, "counters": self.counters}

    def report(self):
        logging.info("=== RUN STATS ===")
        if self.phase_times:
            logging.info("Phase timings (s): %s", json.dumps(self.phase_times, sort_keys=True))
        if self.counters:
            logging.info("Counters: %s", json.dumps(self.counters, sort_keys=True))

    def save_to_jsonl(self, path):
        record = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            **self.as_dict(),
        }
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
            logging.info("Stats appended to %s", path)
        except Exception as exc:
            logging.warning("Could not persist stats to %s: %s", path, exc)

    def finish(self):
        """End any phases still open (e.g. after an exception mid-run)."""
        for name in list(self._phase_start):
            self.end_phase(name)


class OvermortalStateEngine:

    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

    def __init__(self, boss_img_path, icon_img_path, home_img_path, start_img_path=None):
        self.boss_path = boss_img_path
        self.icon_path = icon_img_path
        self.home_path = home_img_path
        self.start_path = start_img_path

        self.boss_template = cv2.imread(self.boss_path, cv2.IMREAD_GRAYSCALE)
        self.icon_template = cv2.imread(self.icon_path, cv2.IMREAD_GRAYSCALE)
        self.home_template = cv2.imread(self.home_path, cv2.IMREAD_GRAYSCALE)

        # Kept separately in color. The fast per-frame checks during scanning
        # stay grayscale (cheap, and fine once the camera is already close to
        # the right spot), but the mosaic-wide search that has to tell apart
        # several visually-similar white icons on a black map needs the real
        # color information -- in grayscale, a blue-white tree icon and a
        # gold-white teardrop icon can look nearly identical.
        self.home_template_color = cv2.imread(self.home_path, cv2.IMREAD_COLOR)

        self.start_template = None
        if self.start_path and os.path.exists(self.start_path):
            self.start_template = cv2.imread(self.start_path, cv2.IMREAD_GRAYSCALE)
            if self.start_template is not None:
                logging.info(
                    "Start template loaded: %s (%dx%d)",
                    os.path.basename(self.start_path),
                    self.start_template.shape[1], self.start_template.shape[0],
                )

        if any(
            img is None
            for img in [
                self.boss_template,
                self.icon_template,
                self.home_template,
            ]
        ):
            raise FileNotFoundError(
                "Critical Error: Missing template graphics files."
            )

        self.adb_port = "5555"
        self.adb_device = f"127.0.0.1:{self.adb_port}"
        self.adb_path = os.path.join(self.SCRIPT_DIR, "platform-tools", "adb.exe")
        self.adb_base = f"{self.adb_path} -s {self.adb_device} shell input"

        # Bookkeeping populated by the grid scanner so a later fallback pass
        # can re-navigate the camera back to whichever cell a match was
        # found in, instead of tapping a fixed screen coordinate.
        self.last_cell_moves = {}
        self.last_mosaic_cell_size = (None, None)
        self.last_duplicate_cells = set()

        # Desktop capture (DXcam) — lazily initialised on first frame
        self._cam = None
        self._bs_hwnd = None

        # Metrics collector for run statistics (see RunStats).
        self.stats = RunStats()

    def get_bluestacks_adb_port(self):
        conf_path = r"C:\ProgramData\BlueStacks_nxt\bluestacks.conf"
        if os.path.exists(conf_path):
            try:
                with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                    match = re.search(r'bst\.instance\..*?\.status\.adb_port="(\d+)"', content)
                    if match:
                        return match.group(1)
            except Exception as e:
                logging.warning(f"Could not read bluestacks.conf: {e}")
        return "5555"

    def launch_bluestacks(self):
        self.stats.start_phase("launch")

        self.adb_port = self.get_bluestacks_adb_port()
        self.adb_device = f"127.0.0.1:{self.adb_port}"
        self.adb_base = f"{self.adb_path} -s {self.adb_device} shell input"

        logging.info(f"Targeting BlueStacks endpoint: {self.adb_device}")

        subprocess.run(f"{self.adb_path} kill-server", shell=True, capture_output=True)
        subprocess.run(f"{self.adb_path} start-server", shell=True, capture_output=True)

        connect_cmd = f"{self.adb_path} connect {self.adb_device}"
        subprocess.run(connect_cmd, capture_output=True, text=True, shell=True)

        check_cmd = f"{self.adb_path} devices"
        result = subprocess.run(check_cmd, capture_output=True, text=True, shell=True)

        is_online = False
        for line in result.stdout.splitlines():
            if self.adb_device in line and "device" in line and "offline" not in line:
                is_online = True
                break

        if not is_online:
            logging.info("BlueStacks is not running. Launching executable...")
            if not os.path.exists(BLUESTACKS_EXE_PATH):
                self.stats.inc("launch_aborted")
                raise FileNotFoundError(f"BlueStacks not found at: {BLUESTACKS_EXE_PATH}")

            subprocess.Popen([BLUESTACKS_EXE_PATH])
            logging.info("BlueStacks process started. Waiting for Android VM to finish booting...")

            connected = False
            for attempt in range(45):
                time.sleep(2.0)
                subprocess.run(connect_cmd, capture_output=True, text=True, shell=True)

                dev_res = subprocess.run(check_cmd, capture_output=True, text=True, shell=True)
                for line in dev_res.stdout.splitlines():
                    if self.adb_device in line and "device" in line and "offline" not in line:
                        connected = True
                        break

                if connected:
                    self.stats.counters["boot_attempts"] = attempt + 1
                    self.stats.inc("boot_success")
                    logging.info(f"ADB handshake established on attempt {attempt + 1}!")
                    break

            if not connected:
                self.stats.inc("boot_timeout")
                raise TimeoutError(
                    "BlueStacks took too long to boot or ADB is disabled in BlueStacks Settings > Advanced."
                )
        else:
            logging.info("BlueStacks is already online and connected.")
            self.stats.inc("boot_online_already")

        logging.info("Waiting for Android OS boot_completed signal...")
        for _ in range(30):
            boot_check = subprocess.run(
                f"{self.adb_path} -s {self.adb_device} shell getprop sys.boot_completed",
                shell=True, capture_output=True, text=True
            )
            if "1" in boot_check.stdout:
                logging.info("Android OS boot completed!")
                break
            time.sleep(2.0)

        time.sleep(5.0)
        self.stats.end_phase("launch")

    def is_game_launched_visually(self):
        frame = self.capture_adb_frame()
        if frame is None:
            return False

        h, w = frame.shape[:2]
        if h > w:
            return True

        check_ps = subprocess.run(
            f"{self.adb_path} -s {self.adb_device} shell ps",
            shell=True, capture_output=True, text=True
        )
        return OVERMORTAL_PACKAGE_NAME in check_ps.stdout

    def launch_overmortal(self):
        logging.info("Attempting to launch Overmortal application...")
        self.stats.start_phase("app_launch")

        logging.info("Tapping Overmortal desktop icon...")
        if not self.scan_and_click_ui_element(self.icon_template, threshold=0.72):
            self.execute_adb_tap(800, 320, delay=0.5)

        monkey_cmd = (
            f"{self.adb_path} -s {self.adb_device} shell monkey "
            f"-p {OVERMORTAL_PACKAGE_NAME} -c android.intent.category.LAUNCHER 1"
        )
        subprocess.run(monkey_cmd, shell=True, capture_output=True)

        logging.info("Monitoring launch state (Polling frame buffer for portrait game mode)...")
        start_time = time.time()
        max_timeout = 60.0
        app_running = False

        while (time.time() - start_time) < max_timeout:
            if self.is_game_launched_visually():
                elapsed = int(time.time() - start_time)
                logging.info(f"Overmortal screen launch verified visually after {elapsed} seconds!")
                app_running = True
                break

            time.sleep(1.5)

        if app_running:
            self.stats.inc("launch_verified")
        else:
            self.stats.inc("launch_timeout")
            logging.warning("Visual check timed out, but proceeding directly to start screen sequence.")

        logging.info("Allowing 12 seconds for game splash assets and notices to load...")
        time.sleep(12.0)
        self.stats.end_phase("app_launch")

    def press_start_game(self):
        logging.info("Waiting for start screen to fully render...")
        self.stats.start_phase("start_game")

        # Dismiss any notice/popup modals obscuring the start button
        for i in range(6):
            self.execute_adb_tap(544, 1781, delay=1.5)

        start_found_via_template = False
        if self.start_template is not None:
            logging.info("Scanning for Start Game button (template match)...")
            for attempt in range(30):
                if self.scan_and_click_ui_element(self.start_template, threshold=0.50):
                    logging.info("Start Game button found and tapped!")
                    start_found_via_template = True
                    break
                # Re-dismiss notices periodically in case new ones pop up
                if attempt % 5 == 4:
                    self.execute_adb_tap(544, 1781, delay=0.5)
                time.sleep(2.0)
            else:
                logging.warning("Start button not found after 60s. Falling back to hardcoded tap.")
                self.execute_adb_tap(537, 1600, delay=5)
        else:
            logging.warning("No start_game_target.png template found. Using hardcoded coordinate.")
            time.sleep(4.0)
            self.execute_adb_tap(537, 1600, delay=5)

        if start_found_via_template:
            self.stats.inc("start_template_found")
        else:
            self.stats.inc("start_fallback")

        logging.info("Start game triggered. Waiting 20 seconds for game assets and world HUD to fully load...")

        load_timer = 20
        for remaining in range(load_timer, 0, -5):
            logging.info(f"Loading into world... {remaining} seconds remaining.")
            self.execute_adb_tap(537, 1600, delay=0.5)
            time.sleep(4.5)

        logging.info("20-second load timer complete! Checking for Main World HUD...")
        gray_scene = self.capture_adb_frame()
        if gray_scene is not None:
            match_matrix = cv2.matchTemplate(gray_scene, self.home_template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(match_matrix)

            match_matrix_icon = cv2.matchTemplate(gray_scene, self.icon_template, cv2.TM_CCOEFF_NORMED)
            _, max_val_icon, _, _ = cv2.minMaxLoc(match_matrix_icon)

            if max_val >= 0.45 or max_val_icon >= 0.45:
                logging.info("SUCCESS: Main World HUD verified!")
                self.stats.inc("hud_verified")
            else:
                logging.warning("HUD template match low, but 20s buffer expired. Proceeding to main task routine.")
                self.stats.inc("hud_unverified")

        logging.info("Game fully loaded. Handing over control to Beast Invasion routine.")
        time.sleep(2.0)
        self.stats.end_phase("start_game")
        return True

    def capture_adb_frame(self, color=False):
        # ---- DXcam desktop capture (lazy single-shot init) ----
        if not getattr(self, '_dxcam_dead', False) and self._cam is None:
            try:
                import dxcam, win32gui
                hwnds = []
                win32gui.EnumWindows(
                    lambda h, _: (hwnds.append(h)
                                  if win32gui.IsWindowVisible(h) and "BlueStacks" in win32gui.GetWindowText(h)
                                  else None) or True,
                    None)
                if hwnds:
                    self._cam = dxcam.create(output_color="BGR")
                    self._bs_hwnd = hwnds[0]
                    logging.info("DXcam ready (BlueStacks HWND=%d)", self._bs_hwnd)
            except Exception:
                self._dxcam_dead = True

        if not getattr(self, '_dxcam_dead', False) and self._cam is not None:
            try:
                import win32gui
                r = win32gui.GetWindowRect(self._bs_hwnd)
                frame = self._cam.grab(region=(r[0], r[1] + 37, r[2], r[3]))
                if frame is not None:
                    return frame if color else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            except Exception:
                self._dxcam_dead = True

        # ---- ADB PNG fallback ----
        cmd = f"{self.adb_path} -s {self.adb_device} exec-out screencap -p"
        image_bytes = subprocess.run(cmd, capture_output=True, shell=True).stdout
        if not image_bytes:
            logging.error("ADB screencap returned empty byte stream.")
            return None
        return cv2.imdecode(np.frombuffer(image_bytes, np.uint8),
                            cv2.IMREAD_COLOR if color else cv2.IMREAD_GRAYSCALE)

    def _multiscale_match(self, scene, template, scales=None):
        if scales is None:
            scales = np.linspace(0.8, 1.2, 9)
        candidates = []
        for scale in scales:
            rw, rh = int(template.shape[1] * scale), int(template.shape[0] * scale)
            if rw > scene.shape[1] or rh > scene.shape[0]:
                continue
            rt = cv2.resize(template, (rw, rh), interpolation=cv2.INTER_AREA)
            mm = cv2.matchTemplate(scene, rt, cv2.TM_CCOEFF_NORMED)
            _, mv, _, ml = cv2.minMaxLoc(mm)
            candidates.append((mv, ml, (rw, rh)))
        if not candidates:
            return -1, None, None
        return max(candidates, key=lambda x: x[0])

    def _find_all_matches(self, scene, template, threshold=0.5, max_matches=6, scales=None):
        if scales is None:
            scales = np.linspace(0.8, 1.2, 9)

        raw_hits = []
        for scale in scales:
            resized_w = int(template.shape[1] * scale)
            resized_h = int(template.shape[0] * scale)

            if resized_h > scene.shape[0] or resized_w > scene.shape[1]:
                continue

            resized_template = cv2.resize(template, (resized_w, resized_h), interpolation=cv2.INTER_AREA)
            match_matrix = cv2.matchTemplate(scene, resized_template, cv2.TM_CCOEFF_NORMED)

            ys, xs = np.where(match_matrix >= threshold)
            for y, x in zip(ys, xs):
                raw_hits.append((float(match_matrix[y, x]), int(x), int(y), resized_w, resized_h))

        raw_hits.sort(key=lambda hit: hit[0], reverse=True)

        accepted = []
        for score, x, y, w, h in raw_hits:
            cx, cy = x + w // 2, y + h // 2
            too_close = False
            for (_, ax, ay, aw, ah) in accepted:
                acx, acy = ax + aw // 2, ay + ah // 2
                dist = ((cx - acx) ** 2 + (cy - acy) ** 2) ** 0.5
                if dist < min(w, h, aw, ah) * 0.6:
                    too_close = True
                    break
            if not too_close:
                accepted.append((score, x, y, w, h))
            if len(accepted) >= max_matches:
                break

        return accepted

    def scan_and_click_ui_element(self, template, threshold=0.72):
        gray_scene = self.capture_adb_frame()
        if gray_scene is None:
            return False

        cv2.imwrite(os.path.join(self.SCRIPT_DIR, "debug_live_capture.png"), gray_scene)
        logging.debug(
            "Captured frame: %dx%d — template: %dx%d — best score: (pending)",
            gray_scene.shape[1], gray_scene.shape[0],
            template.shape[1], template.shape[0],
        )

        best_val, best_loc, best_match_shape = self._multiscale_match(gray_scene, template)

        logging.info(f"Grayscale Match Score: {best_val:.3f}")

        if best_val >= threshold:
            w, h = best_match_shape
            center_x = best_loc[0] + (w // 2)
            center_y = best_loc[1] + (h // 2)
            logging.info(f"TARGET LOCATED (Grayscale)! Score: {best_val:.2f} at ({center_x}, {center_y})")
            self.execute_adb_tap(center_x, center_y)
            return True

        template_edges = cv2.Canny(template, 50, 150)
        scene_edges = cv2.Canny(gray_scene, 50, 150)
        best_edge_val, best_edge_loc, best_edge_shape = self._multiscale_match(scene_edges, template_edges)

        logging.info(f"Edge Match Fallback Score: {best_edge_val:.3f}")

        if best_edge_val >= 0.42:
            w, h = best_edge_shape
            center_x = best_edge_loc[0] + (w // 2)
            center_y = best_edge_loc[1] + (h // 2)
            logging.info(f"TARGET LOCATED (Edge Match)! Score: {best_edge_val:.2f} at ({center_x}, {center_y})")
            self.execute_adb_tap(center_x, center_y)
            return True

        return False

    def execute_adb_tap(self, x, y, delay=2.0):
        logging.info(f"Navigating UI: Tapping coordinate ({x}, {y})")
        subprocess.run(f"{self.adb_base} tap {x} {y}", shell=True, capture_output=True)
        time.sleep(delay)

    def execute_adb_swipe(self, x1, y1, x2, y2, duration=300, delay=0.5):
        subprocess.run(f"{self.adb_base} swipe {x1} {y1} {x2} {y2} {duration}", shell=True, capture_output=True)
        time.sleep(delay)

    def calibrate_to_origin(self):
        logging.info("Resetting camera position to Top-Left Origin (0,0)...")
        for _ in range(4):
            self.execute_adb_swipe(400, 400, 1400, 400, duration=200, delay=0.3)
            self.execute_adb_swipe(960, 400, 960, 1400, duration=200, delay=0.3)
        time.sleep(1.0)

    def _cleanup_temp_frames(self, grid_dict):
        for path in grid_dict.values():
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception as e:
                    logging.warning(f"Could not remove temp frame {path}: {e}")

    def _cleanup_artifacts(self):
        """Remove all intermediate screen captures from disk."""
        import glob as _glob
        patterns = [
            os.path.join(self.SCRIPT_DIR, "debug_live_capture.png"),
            os.path.join(self.SCRIPT_DIR, "full_map_stitched.png"),
            os.path.join(self.SCRIPT_DIR, "temp_grid_*.png"),
        ]
        for pattern in patterns:
            for path in _glob.glob(pattern):
                try:
                    os.remove(path)
                    logging.debug("Cleaned up %s", path)
                except Exception:
                    pass

    def navigate_and_scan_boss(self, map_node_tap_function, node_name):
        logging.info(f"Opening Beast Invasion event interface for {node_name}...")
        self.stats.start_phase("boss_scan")
        self.execute_adb_tap(971, 57)
        self.execute_adb_tap(1000, 300)

        logging.info("Scanning for shifting Beast Invasion menu icon...")
        icon_found = False
        for _ in range(5):
            if self.scan_and_click_ui_element(self.icon_template, threshold=0.72):
                logging.info("Beast Invasion menu icon located dynamically!")
                icon_found = True
                time.sleep(2.0)
                break
            time.sleep(1.0)

        if not icon_found:
            logging.warning(f"Invasion icon absent. Skipping branch: {node_name}.")
            self.stats.inc("invasion_icon_missing")
            self.stats.end_phase("boss_scan")
            self.execute_adb_tap(450, 1810)
            return False

        self.stats.inc("invasion_icon_found")
        map_node_tap_function()
        time.sleep(4.0)

        boss_engaged = self.boustrophedon_zigzag_search_expanded(self.boss_template, threshold=0.72)
        if boss_engaged:
            self.stats.inc("boss_found")
            self.stats.inc("boss_tier_live")
            self.execute_adb_tap(820, 1560)
            self.stats.end_phase("boss_scan")
            return True

        self.stats.inc("boss_missed")
        self.execute_adb_tap(450, 1810)
        self.stats.end_phase("boss_scan")
        return False

    def return_to_immortal_world(self):
        logging.info("All scans finalized. Returning home to Immortal World...")
        self.stats.start_phase("return_home")
        self.execute_adb_tap(971, 57)
        self.execute_adb_tap(1000, 300)
        self.execute_adb_tap(268, 1825)
        self.execute_adb_tap(80, 1290)
        time.sleep(3.0)

        home_resolved = False
        if self.boustrophedon_zigzag_search_expanded(self.home_template, total_rows=4, micro_steps_per_row=6, threshold=0.70):
            logging.info("Solvarian Domain located and tapped during live scan.")
            self.stats.inc("home_tier_live")
            home_resolved = True
        else:
            logging.info("Live scan missed target, checking fully stitched map mosaic...")
            stitch_path = os.path.join(self.SCRIPT_DIR, "full_map_stitched.png")
            if os.path.exists(stitch_path):
                stitched_map = cv2.imread(stitch_path, cv2.IMREAD_COLOR)
                # Raised threshold slightly to 0.55 to help filter out weaker background overlaps
                candidates = self._find_all_matches(stitched_map, self.home_template_color, threshold=0.55)

                if not candidates:
                    logging.error("Could not locate Solvarian Domain asset in the stitched mosaic (no candidates >= 0.55).")
                    self.stats.inc("home_mosaic_no_match")
                elif len(candidates) > 1 and (candidates[0][0] - candidates[1][0]) < 0.01:
                    logging.warning(
                        f"AMBIGUOUS MATCH: top {len(candidates)} candidates scored within 0.01 of each other "
                        f"(best={candidates[0][0]:.2f}, next={candidates[1][0]:.2f}), even with color matching. "
                        "Either several map icons genuinely share similar color+shape, or the template still "
                        "includes too much non-distinctive background/border. Skipping the tap instead of "
                        "guessing -- each candidate below is resolved to its scanned grid cell/row so you can "
                        "cross-check it directly against the numbered scan screenshots."
                    )
                    self.stats.inc("home_mosaic_ambiguous")
                    cell_w, cell_h = self.last_mosaic_cell_size
                    for i, (score, x, y, w, h) in enumerate(candidates):
                        center_x = x + w // 2
                        center_y = y + h // 2
                        if cell_w and cell_h:
                            col = int(center_x // cell_w) + 1
                            row = int(center_y // cell_h) + 1
                            dup_note = "  [known boundary-duplicate cell -- may be a repeat, not a distinct location]" if (row, col) in self.last_duplicate_cells else ""
                            logging.info(
                                f"  Candidate {i + 1}: score={score:.2f} -> grid cell (row={row}, col={col}), "
                                f"mosaic pixel ({center_x}, {center_y}){dup_note}"
                            )
                        else:
                            logging.info(f"  Candidate {i + 1}: score={score:.2f} at mosaic pixel ({center_x}, {center_y})")
                else:
                    max_val, mx, my, match_w, match_h = candidates[0]
                    max_loc = (mx, my)
                    logging.info(f"Target found in stitched mosaic with score {max_val:.2f} (unambiguous)! Resolving grid cell...")

                    cell_w, cell_h = self.last_mosaic_cell_size
                    target_cell = None
                    if cell_w and cell_h:
                        center_x = max_loc[0] + match_w // 2
                        center_y = max_loc[1] + match_h // 2
                        col = int(center_x // cell_w) + 1
                        row = int(center_y // cell_h) + 1
                        target_cell = (row, col)
                        logging.info(f"Mosaic match maps to scanned grid cell (row={row}, col={col}).")

                    moves = self.last_cell_moves.get(target_cell) if target_cell else None

                    if moves is not None:
                        logging.info("Re-navigating camera to that grid cell...")
                        self.calibrate_to_origin()
                        for (x1, y1, x2, y2, duration) in moves:
                            self.execute_adb_swipe(x1, y1, x2, y2, duration=duration, delay=0.4)
                        time.sleep(0.8)

                        # Directly translate the mosaic coordinate position within the cell 
                        # to avoid secondary local match drift.
                        cell_w, cell_h = self.last_mosaic_cell_size
                        if cell_w and cell_h:
                            local_x_in_cell = max_loc[0] % cell_w
                            local_y_in_cell = max_loc[1] % cell_h
                            
                            # Map local cell coordinates back to standard viewport scale (e.g., 1080x1920 reference)
                            # Adjusting for cropped borders if necessary
                            target_x = int(local_x_in_cell) + 150  # Center offset adjustment based on pan range
                            target_y = int(local_y_in_cell) + 200
                            
                            logging.info(f"Tapping resolved target coordinate directly at ({target_x}, {target_y})")
                            self.stats.inc("home_tier_mosaic")
                            home_resolved = True
                            self.execute_adb_tap(target_x, target_y)
                            time.sleep(1.0)
                            self.execute_adb_tap(820, 1560)
                        else:
                            # Fallback to cell center if dimensions are missing
                            self.stats.inc("home_tier_mosaic")
                            home_resolved = True
                            self.execute_adb_tap(540, 960)
                            time.sleep(0.5)
                            self.execute_adb_tap(820, 1560)
                    else:
                        logging.warning(
                            "Could not map the mosaic match back to a scanned grid cell "
                            "(no recorded camera moves for it). Skipping unreliable fixed-coordinate tap."
                        )
            else:
                logging.error("Could not locate Solvarian Domain asset via dense matrix checks.")

        if home_resolved:
            self.stats.inc("home_success")
        else:
            self.stats.inc("home_missed")

        self.stats.end_phase("return_home")
        self._cleanup_artifacts()
        logging.info("Safe baseline state processing complete.")

    def boustrophedon_zigzag_search_expanded(self, target_template, total_rows=4, micro_steps_per_row=6, threshold=0.72):
        self.calibrate_to_origin()
        grid_frames = {}
        cell_moves = {}
        move_log = []

        self.last_cell_moves = {}
        self.last_mosaic_cell_size = (None, None)
        self.last_duplicate_cells = set()

        def record_move(x1, y1, x2, y2, duration=300, delay=0.4):
            move_log.append((x1, y1, x2, y2, duration))
            self.execute_adb_swipe(x1, y1, x2, y2, duration=duration, delay=delay)

        last_frame_signature = {"gray": None}

        def frame_looks_like_previous(gray_frame, tolerance=3.0):
            small = cv2.resize(gray_frame, (80, 140), interpolation=cv2.INTER_AREA).astype(np.float32)
            prev = last_frame_signature["gray"]
            last_frame_signature["gray"] = small
            if prev is None:
                return False
            return float(np.mean(np.abs(small - prev))) < tolerance

        def save_current_frame(r, s):
            color_frame = self.capture_adb_frame(color=True)
            if color_frame is None:
                return None
            gray_frame = cv2.cvtColor(color_frame, cv2.COLOR_BGR2GRAY)

            if frame_looks_like_previous(gray_frame):
                logging.warning(
                    f"Frame at grid cell (row={r}, col={s}) is nearly identical to the previous "
                    "capture -- the camera likely hit a map boundary and couldn't pan further. "
                    "This cell will still be stitched, but expect duplicate content in the mosaic here."
                )
                self.last_duplicate_cells.add((r, s))
                self.stats.inc("duplicate_cells")

            path = os.path.join(self.SCRIPT_DIR, f"temp_grid_{r}_{s}.png")
            cv2.imwrite(path, color_frame)
            grid_frames[(r, s)] = path
            cell_moves[(r, s)] = list(move_log)
            return gray_frame

        def check_and_tap_if_found(frame, r, s):
            match_matrix = cv2.matchTemplate(frame, target_template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(match_matrix)
            if max_val >= threshold:
                th, tw = target_template.shape[:2]
                center_x = max_loc[0] + tw // 2
                center_y = max_loc[1] + th // 2
                logging.info(
                    f"Target found live at row {r}, step {s} (score {max_val:.2f})! Tapping live coordinate ({center_x}, {center_y})..."
                )
                self.execute_adb_tap(center_x, center_y)
                self.stitch_grid_map(grid_frames, total_rows, micro_steps_per_row)
                self._cleanup_temp_frames(grid_frames)
                return True
            return False

        initial_gray = save_current_frame(1, 1)
        if initial_gray is not None and check_and_tap_if_found(initial_gray, 1, 1):
            return True

        for row in range(1, total_rows + 1):
            logging.info(f"Scanning Matrix Row {row}/{total_rows}...")

            if row % 2 != 0:
                x_start, x_end = 700, 380
            else:
                x_start, x_end = 380, 700

            for step in range(2, micro_steps_per_row + 1):
                record_move(x_start, 960, x_end, 960, duration=300, delay=0.4)
                time.sleep(0.8)

                frame = save_current_frame(row, step)
                if frame is not None and check_and_tap_if_found(frame, row, step):
                    return True

            if row < total_rows:
                logging.info(f"Shifting Camera Downward to Row {row + 1}...")
                record_move(540, 1200, 540, 800, duration=300, delay=0.4)
                time.sleep(0.8)

                frame = save_current_frame(row + 1, 1)
                if frame is not None and check_and_tap_if_found(frame, row + 1, 1):
                    return True

        logging.info("Scan completed without live match. Generating full grid map mosaic...")
        self.stitch_grid_map(grid_frames, total_rows, micro_steps_per_row)
        self.last_cell_moves = cell_moves
        self._cleanup_temp_frames(grid_frames)
        return False

    def stitch_grid_map(self, grid_dict, total_rows, total_cols):
        rows_to_concat = []
        TOP_CROP = 130
        BOTTOM_CROP = 220

        sample_cell_w, sample_cell_h = None, None

        for r in range(1, total_rows + 1):
            row_images = []
            for c in range(1, total_cols + 1):
                if (r, c) in grid_dict:
                    img = cv2.imread(grid_dict[(r, c)])
                    if img is not None:
                        h, w = img.shape[:2]
                        if h - BOTTOM_CROP <= TOP_CROP:
                            logging.warning(
                                "Skipping grid frame %s: too short (%dpx) for crop offsets",
                                grid_dict[(r, c)], h,
                            )
                            continue
                        cropped_img = img[TOP_CROP : h - BOTTOM_CROP, 0:w]
                        row_images.append(cropped_img)
            
            # Correctly reverse even rows back to sequential left-to-right display orientation
            if r % 2 == 0:
                row_images = row_images[::-1]

            if row_images:
                min_height = min(img.shape[0] for img in row_images)
                resized_row_imgs = []
                for img in row_images:
                    resized_w = int(img.shape[1] * min_height / img.shape[0])
                    if resized_w < 1:
                        continue
                    resized_row_imgs.append(cv2.resize(img, (resized_w, min_height)))
                if not resized_row_imgs:
                    continue
                row_stitched = np.hstack(resized_row_imgs)
                if sample_cell_w is None and len(resized_row_imgs) > 0:
                    sample_cell_w = resized_row_imgs[0].shape[1]
                    sample_cell_h = min_height
                rows_to_concat.append(row_stitched)

        if not rows_to_concat:
            logging.warning("No valid images found for grid mosaic stitching.")
            return False

        min_width = min(row.shape[1] for row in rows_to_concat)
        resized_col_rows = [cv2.resize(row, (min_width, int(row.shape[0] * min_width / row.shape[1]))) for row in rows_to_concat]
        
        scale_ratio = min_width / rows_to_concat[0].shape[1] if rows_to_concat else 1.0
        if sample_cell_w and sample_cell_h:
            self.last_mosaic_cell_size = (int(sample_cell_w * scale_ratio), int(sample_cell_h * scale_ratio))

        final_mosaic = np.vstack(resized_col_rows)

        output_path = os.path.join(self.SCRIPT_DIR, "full_map_stitched.png")
        cv2.imwrite(output_path, final_mosaic)
        logging.info(f"SUCCESS: Clean grid matrix map successfully stitched and saved as '{output_path}'!")
        return True


if __name__ == "__main__":
    SCRIPT_DIR = OvermortalStateEngine.SCRIPT_DIR
    bot = OvermortalStateEngine(
        boss_img_path=os.path.join(SCRIPT_DIR, "boss_target.png"),
        icon_img_path=os.path.join(SCRIPT_DIR, "invasion_icon.png"),
        home_img_path=os.path.join(SCRIPT_DIR, "home_target.png"),
        start_img_path=os.path.join(SCRIPT_DIR, "start_game_target.png"),
    )

    try:
        bot.stats.start_phase("total")
        bot.launch_bluestacks()
        bot.launch_overmortal()
        bot.press_start_game()
        bot.return_to_immortal_world()
        bot.stats.inc("run_completed")
    finally:
        bot.stats.end_phase("total")
        bot.stats.finish()
        bot.stats.report()
        bot.stats.save_to_jsonl(os.path.join(SCRIPT_DIR, "run_results.jsonl"))
        bot._cleanup_artifacts()
        logging.info("Execution finished.")
