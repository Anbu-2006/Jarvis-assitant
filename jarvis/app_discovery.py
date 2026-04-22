"""
JARVIS App Discovery — Auto-scan ALL installed applications on Windows.

Scans:
1. Start Menu shortcuts (.lnk files)
2. Registry uninstall entries
3. Common installation directories
4. PATH executables
5. UWP/Store apps

Outputs a JSON map of app_name → executable_path for JARVIS to use.
"""

import os
import json
import glob
import shutil
import subprocess
import winreg
import logging
from pathlib import Path

log = logging.getLogger("jarvis.app_discovery")

def _resolve_lnk(lnk_path: str) -> str | None:
    """Resolve a .lnk shortcut to its target path using PowerShell."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk_path}').TargetPath"],
            capture_output=True, text=True, timeout=5
        )
        target = result.stdout.strip()
        if target and os.path.isfile(target):
            return target
    except Exception:
        pass
    return None


def scan_start_menu() -> dict[str, str]:
    """Scan Start Menu for all application shortcuts."""
    apps = {}
    start_menu_dirs = [
        os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
        os.path.expandvars(r"%PROGRAMDATA%\Microsoft\Windows\Start Menu\Programs"),
    ]
    
    for base_dir in start_menu_dirs:
        if not os.path.isdir(base_dir):
            continue
        for root, dirs, files in os.walk(base_dir):
            for f in files:
                if f.lower().endswith(".lnk"):
                    name = f[:-4].lower().strip()
                    # Skip uninstallers and updaters
                    if any(skip in name for skip in ["uninstall", "update", "readme", "help", "manual", "license"]):
                        continue
                    lnk_path = os.path.join(root, f)
                    target = _resolve_lnk(lnk_path)
                    if target:
                        apps[name] = target
    return apps


def scan_registry() -> dict[str, str]:
    """Scan Windows registry for installed applications."""
    apps = {}
    reg_paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    
    for hive, path in reg_paths:
        try:
            key = winreg.OpenKey(hive, path)
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    subkey_name = winreg.EnumKey(key, i)
                    subkey = winreg.OpenKey(key, subkey_name)
                    try:
                        name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                        try:
                            icon = winreg.QueryValueEx(subkey, "DisplayIcon")[0]
                            # DisplayIcon often points to the .exe
                            exe_path = icon.split(",")[0].strip('"').strip()
                            if exe_path and os.path.isfile(exe_path) and exe_path.lower().endswith(".exe"):
                                apps[name.lower().strip()] = exe_path
                        except (FileNotFoundError, OSError):
                            try:
                                install_loc = winreg.QueryValueEx(subkey, "InstallLocation")[0]
                                if install_loc and os.path.isdir(install_loc):
                                    # Find main .exe in install directory
                                    exes = glob.glob(os.path.join(install_loc, "*.exe"))
                                    if exes:
                                        apps[name.lower().strip()] = exes[0]
                            except (FileNotFoundError, OSError):
                                pass
                    except (FileNotFoundError, OSError):
                        pass
                    finally:
                        winreg.CloseKey(subkey)
                except (OSError, PermissionError):
                    continue
            winreg.CloseKey(key)
        except (OSError, PermissionError):
            continue
    return apps


def scan_common_dirs() -> dict[str, str]:
    """Scan common installation directories."""
    apps = {}
    dirs_to_scan = [
        os.path.expandvars(r"%PROGRAMFILES%"),
        os.path.expandvars(r"%PROGRAMFILES(x86)%"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs"),
    ]
    
    for base in dirs_to_scan:
        if not os.path.isdir(base):
            continue
        try:
            for entry in os.scandir(base):
                if entry.is_dir():
                    # Look for a main .exe
                    exes = glob.glob(os.path.join(entry.path, "*.exe"))
                    if exes:
                        name = entry.name.lower()
                        # Pick the exe that best matches the folder name
                        best = None
                        for exe in exes:
                            exe_name = os.path.basename(exe).lower()
                            if name in exe_name or exe_name.replace(".exe", "") in name:
                                best = exe
                                break
                        apps[name] = best or exes[0]
        except PermissionError:
            continue
    return apps


def scan_uwp_apps() -> dict[str, str]:
    """Scan UWP/Store apps via PowerShell."""
    apps = {}
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-AppxPackage | Where-Object {$_.IsFramework -eq $false} | "
             "Select-Object Name, PackageFamilyName | ConvertTo-Json"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            if isinstance(data, dict):
                data = [data]
            for app in data:
                name = app.get("Name", "").lower()
                pfn = app.get("PackageFamilyName", "")
                if name and pfn and not any(skip in name for skip in [
                    "framework", "runtime", "vclibs", "appx", "extension",
                    "microsoft.ui", "microsoft.net", "microsoft.windows"
                ]):
                    # UWP apps can be launched via shell:AppsFolder\{PFN}!App
                    apps[name] = f"shell:AppsFolder\\{pfn}!App"
    except Exception as e:
        log.warning(f"UWP scan failed: {e}")
    return apps


def discover_all_apps() -> dict[str, str]:
    """Run all scanners and merge results.
    
    Returns a deduplicated dict of {app_name: executable_path}.
    """
    all_apps = {}
    
    # Priority order: Start Menu > Registry > Common dirs > UWP
    print("Scanning Start Menu shortcuts...")
    all_apps.update(scan_common_dirs())
    print(f"  Found {len(all_apps)} apps so far")
    
    print("Scanning registry...")
    all_apps.update(scan_registry())
    print(f"  Found {len(all_apps)} apps so far")
    
    print("Scanning Start Menu...")
    all_apps.update(scan_start_menu())
    print(f"  Found {len(all_apps)} apps so far")

    # Also add PATH executables for common tools
    common_tools = [
        "python", "python3", "node", "npm", "git", "code", "cursor",
        "docker", "kubectl", "terraform", "aws", "gcloud", "az",
        "ffmpeg", "vlc", "ssh", "scp", "curl", "wget",
    ]
    for tool in common_tools:
        path = shutil.which(tool)
        if path:
            all_apps[tool] = path
    
    # Clean up names
    cleaned = {}
    for name, path in all_apps.items():
        # Normalize name
        clean_name = name.strip().lower()
        # Skip empty or system entries
        if not clean_name or len(clean_name) < 2:
            continue
        cleaned[clean_name] = path
    
    return dict(sorted(cleaned.items()))


def save_app_map(output_path: str = None):
    """Scan all apps and save to JSON."""
    if output_path is None:
        output_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "tools", "data", "app_paths.json"
        )
    
    apps = discover_all_apps()
    
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(apps, f, indent=2, ensure_ascii=False)
    
    print(f"\n{'='*60}")
    print(f"  Discovered {len(apps)} applications")
    print(f"  Saved to: {output_path}")
    print(f"{'='*60}")
    
    # Show top 30
    print("\nTop 30 apps found:")
    for i, (name, path) in enumerate(list(apps.items())[:30], 1):
        print(f"  {i:2}. {name:30s} → {path[:60]}")
    
    return apps


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    apps = save_app_map()
