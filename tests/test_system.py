"""
JARVIS End-to-End System Test v2
Tests 120+ real-world voice commands through the entire pipeline.
"""
import sys, os, shutil, asyncio
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from jarvis.api.main import (
    detect_action_fast, _get_instant_response, extract_action,
    _WINDOWS_APP_MAP, _WINDOWS_APP_PATHS,
)

PASS = 0
FAIL = 0
RESULTS = []

def test(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        RESULTS.append(f"  [PASS] {name}")
    else:
        FAIL += 1
        RESULTS.append(f"  [FAIL] {name} -- {detail}")

def section(title):
    RESULTS.append(f"\n{'='*60}")
    RESULTS.append(f"  {title}")
    RESULTS.append(f"{'='*60}")

# ==========================================================
# 1. INSTANT CACHE
# ==========================================================
section("1. INSTANT RESPONSE CACHE")
for phrase in ["hey jarvis", "hi jarvis", "hello jarvis", "good morning",
               "good evening", "good night", "thank you", "thanks",
               "okay", "ok", "who are you", "what's your name",
               "jarvis", "are you there", "cancel", "stop", "hey travis"]:
    test(f"Instant: '{phrase}'", _get_instant_response(phrase) is not None, "Missing from cache")

for phrase in ["open chrome", "what time is it", "build me a website"]:
    test(f"NOT instant: '{phrase}'", _get_instant_response(phrase) is None, "Wrongly cached")

# ==========================================================
# 2. APP OPENING
# ==========================================================
section("2. APP OPENING DETECTION")
app_tests = [
    "open chrome", "open notepad", "launch vs code", "open calculator",
    "open file explorer", "open task manager", "launch spotify",
    "open settings", "start discord", "open word", "run excel",
    "open powerpoint", "open outlook", "launch slack", "open telegram",
    "open vlc", "open android studio", "open winrar", "open capcut",
    "open onedrive", "open onenote", "hey jarvis whatsapp"
]
for cmd in app_tests:
    result = detect_action_fast(cmd)
    test(f"App: '{cmd}'", result and result["action"] == "open_app", f"Got: {result}")

# ==========================================================
# 3. TERMINAL & SCREEN
# ==========================================================
section("3. TERMINAL / SCREEN / CALENDAR / MAIL")
test("open terminal", detect_action_fast("open terminal") and detect_action_fast("open terminal")["action"] == "open_terminal")
test("open a terminal", detect_action_fast("open a terminal") and detect_action_fast("open a terminal")["action"] == "open_terminal")
test("what's on my screen", detect_action_fast("what's on my screen") and detect_action_fast("what's on my screen")["action"] == "describe_screen")
test("what's my schedule", detect_action_fast("what's my schedule") and detect_action_fast("what's my schedule")["action"] == "check_calendar")
test("check my email", detect_action_fast("check my email") and detect_action_fast("check my email")["action"] == "check_mail")
test("my tasks", detect_action_fast("my tasks") and detect_action_fast("my tasks")["action"] == "check_tasks")

# ==========================================================
# 4. GUI CONTROLS
# ==========================================================
section("4. GUI CONTROLS")
gui_tests = [
    ("scroll down", "gui_scroll"), ("scroll up", "gui_scroll"),
    ("close this window", "gui_close_window"),
    ("minimize", "gui_minimize"), ("maximize", "gui_maximize"),
    ("volume up", "gui_volume"), ("volume down", "gui_volume"), ("mute", "gui_volume"),
    ("lock my computer", "gui_lock"),
    ("open my documents", "gui_open_folder"), ("open downloads", "gui_open_folder"),
]
for cmd, expected in gui_tests:
    result = detect_action_fast(cmd)
    test(f"GUI: '{cmd}' -> {expected}", result and result["action"] == expected, f"Got: {result}")

# ==========================================================
# 5. BROWSER CONTROL
# ==========================================================
section("5. BROWSER CONTROL")
browser_tests = [
    ("new tab", "gui_browser"), ("close tab", "gui_browser"),
    ("go back", "gui_browser"), ("go forward", "gui_browser"),
    ("refresh", "gui_browser"), ("reload page", "gui_browser"),
]
for cmd, expected in browser_tests:
    result = detect_action_fast(cmd)
    test(f"Browser: '{cmd}'", result and result["action"] == expected, f"Got: {result}")

# ==========================================================
# 6. CLIPBOARD
# ==========================================================
section("6. CLIPBOARD SHORTCUTS")
clip_tests = [
    ("copy that", "copy"), ("paste that", "paste"),
    ("select all", "select_all"), ("undo", "undo"),
    ("save file", "save"),
]
for cmd, expected_target in clip_tests:
    result = detect_action_fast(cmd)
    test(f"Clipboard: '{cmd}'", result and result["action"] == "gui_clipboard" and result.get("target") == expected_target, f"Got: {result}")

# ==========================================================
# 7. SYSTEM INFO COMMANDS
# ==========================================================
section("7. SYSTEM INFO / POWER COMMANDS")
sys_tests = [
    ("what's my ip", "run_cmd"), ("how much ram", "run_cmd"),
    ("disk space", "run_cmd"), ("battery level", "run_cmd"),
    ("what time is it", "run_cmd"), ("empty recycle bin", "run_cmd"),
    ("wifi name", "run_cmd"), ("who am i", "run_cmd"),
    ("running processes", "run_cmd"),
    ("shutdown computer", "run_cmd"), ("restart computer", "run_cmd"),
    ("cancel shutdown", "run_cmd"),
]
for cmd, expected in sys_tests:
    result = detect_action_fast(cmd)
    test(f"System: '{cmd}'", result and result["action"] == expected, f"Got: {result}")

# ==========================================================
# 8. SCREENSHOT / DESKTOP / SNAP
# ==========================================================
section("8. SCREENSHOT / DESKTOP / SNAP / TAB")
test("take a screenshot", detect_action_fast("take a screenshot") and detect_action_fast("take a screenshot")["action"] == "gui_screenshot")
test("show desktop", detect_action_fast("show desktop") and detect_action_fast("show desktop")["action"] == "gui_show_desktop")
test("switch window", detect_action_fast("switch window") and detect_action_fast("switch window")["action"] == "gui_alt_tab")
test("snap left", detect_action_fast("snap left") and detect_action_fast("snap left")["action"] == "gui_snap")
test("snap right", detect_action_fast("snap right") and detect_action_fast("snap right")["action"] == "gui_snap")

# ==========================================================
# 9. TYPE / CLICK / PRESS KEY
# ==========================================================
section("9. TYPE / CLICK / PRESS KEY")
test("type hello world", detect_action_fast("type hello world") and detect_action_fast("type hello world")["action"] == "gui_type")
r = detect_action_fast("click at 500 300")
test("click at 500 300", r and r["action"] == "gui_click" and r.get("target") == "500,300", f"Got: {r}")
test("click here", detect_action_fast("click here") and detect_action_fast("click here")["action"] == "gui_click")
test("press enter", detect_action_fast("press enter") and detect_action_fast("press enter")["action"] == "gui_press_key")
test("press escape", detect_action_fast("press escape") and detect_action_fast("press escape")["action"] == "gui_press_key")

# ==========================================================
# 10. WEB SEARCH
# ==========================================================
section("10. WEB SEARCH")
search_tests = [
    "search for python tutorials", "what's the weather in chennai",
    "latest news on AI", "who is elon musk", "look up machine learning",
    "google react tutorial",
]
for cmd in search_tests:
    result = detect_action_fast(cmd)
    test(f"Search: '{cmd}'", result and result["action"] == "web_search", f"Got: {result}")

# ==========================================================
# 11. LLM ACTION TAG EXTRACTION
# ==========================================================
section("11. LLM ACTION TAG EXTRACTION")
tag_tests = [
    ("Right away. [ACTION:OPEN_APP] chrome", "open_app"),
    ("Checking. [ACTION:RUN_COMMAND] ipconfig", "run_command"),
    ("On it. [ACTION:BUILD] weather dashboard", "build"),
    ("Looking. [ACTION:SCREEN]", "screen"),
    ("Noted. [ACTION:REMEMBER] User prefers dark mode", "remember"),
    ("Connecting. [ACTION:PROMPT_PROJECT] myapp ||| Review", "prompt_project"),
    ("Emptying now. [ACTION:RUN_COMMAND] Clear-RecycleBin -Force", "run_command"),
    ("Done. [ACTION:BROWSE] https://google.com", "browse"),
]
for response, expected in tag_tests:
    _, action = extract_action(response)
    test(f"Tag: [ACTION:{expected.upper()}]", action and action["action"] == expected, f"Got: {action}")

# ==========================================================
# 12. NEGATIVE TESTS (should NOT match)
# ==========================================================
section("12. NEGATIVE TESTS (should go to LLM)")
negatives = [
    "tell me a joke", "what is machine learning",
    "how are you doing today", "explain quantum computing to me",
    "I'm thinking about building a new project",
    "can you help me with my homework",
    "what should I eat for dinner",
    "that's interesting tell me more",
]
for cmd in negatives:
    result = detect_action_fast(cmd)
    test(f"NOT action: '{cmd}'", result is None, f"Incorrectly got: {result}")

# ==========================================================
# 13. APP RESOLUTION (can we find executables?)
# ==========================================================
section("13. APP RESOLUTION")
import json
json_path = Path(__file__).parent.parent / "jarvis" / "tools" / "data" / "app_paths.json"
if json_path.exists():
    with open(json_path) as f:
        discovered = json.load(f)
    test(f"app_paths.json loaded ({len(discovered)} apps)", len(discovered) > 50)
    
    critical_apps = ["google chrome", "discord", "word", "excel", "powerpoint", "android studio", "vlc media player", "winrar"]
    for app in critical_apps:
        found = app in discovered and os.path.isfile(discovered[app])
        test(f"App '{app}' resolvable", found, "Not found in app_paths.json" if not found else "")
else:
    test("app_paths.json exists", False, "File not found — run app_discovery.py first")

# ==========================================================
# 14. SPOTIFY PLAY
# ==========================================================
section("14. SPOTIFY PLAY")
spotify_tests = [
    ("play shape of you on spotify", "spotify_play"),
    ("play despacito", "spotify_play"),
    ("play some music", "spotify_play"),
    ("play the song bohemian rhapsody", "spotify_play"),
]
for cmd, expected in spotify_tests:
    result = detect_action_fast(cmd)
    test(f"Spotify: '{cmd}'", result and result["action"] == expected, f"Got: {result}")

# ==========================================================
# 15. CROSS-APP PASTE
# ==========================================================
section("15. CROSS-APP PASTE")
paste_tests = [
    ("paste into notepad", "cross_app_paste"),
    ("paste in antigravity", "cross_app_paste"),
    ("enter into word", "cross_app_paste"),
    ("type into chrome", "cross_app_paste"),
]
for cmd, expected in paste_tests:
    result = detect_action_fast(cmd)
    test(f"Cross-paste: '{cmd}'", result and result["action"] == expected, f"Got: {result}")

# ==========================================================
# SUMMARY
# ==========================================================
print("\n" + "=" * 60)
print("  JARVIS SYSTEM TEST v2 — RESULTS")
print("=" * 60)
for line in RESULTS:
    print(line)
print(f"\n{'='*60}")
print(f"  TOTAL: {PASS + FAIL} tests | PASSED: {PASS} | FAILED: {FAIL}")
print(f"{'='*60}")

if FAIL > 0:
    print(f"\n  {FAIL} FAILURES NEED FIXING")
    sys.exit(1)
else:
    print("\n  ALL TESTS PASSED!")
