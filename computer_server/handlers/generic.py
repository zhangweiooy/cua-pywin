"""
Generic handlers for all OSes.

Includes:
- DesktopHandler
- FileHandler

"""

import base64
import ctypes
import logging
import os
import platform
import subprocess
import webbrowser
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

from .base import BaseDesktopHandler, BaseFileHandler, BaseWindowHandler
from .screen_geometry import get_virtual_screen_geometry
from computer_server.services.permission_service import (
    RuntimePermissionPolicy,
    audit_path as _audit_path,
    resolve_allowed_path,
    resolve_path,
)


# ---------------------------------------------------------------------------
# Shared window filter — used by both list_windows and get_app_windows
# ---------------------------------------------------------------------------

IS_WINDOWS_PLATFORM = platform.system() == "Windows"


def _is_meaningful_window(hwnd: int) -> bool:
    """Conservative filter: only exclude definite noise, keep everything else.

    Excludes:
      - WS_CHILD controls (buttons, edit fields — internal parts of a parent)
      - Zero-size windows

    Keeps: tool windows, dialogs, popovers, empty-title windows, minimized
    windows.  Visibility is handled by each caller (e.g. on_screen_only flag)
    because IsWindowVisible and WS_VISIBLE have different semantics.
    """
    if not IS_WINDOWS_PLATFORM:
        return True  # non-Windows: caller's own filtering applies
    try:
        import win32gui
        import win32con

        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        if style & win32con.WS_CHILD:
            return False
        rect = win32gui.GetWindowRect(hwnd)
        if rect[2] - rect[0] <= 0 or rect[3] - rect[1] <= 0:
            return False
        return True
    except Exception:
        return True  # can't determine → keep it

try:
    import pywinctl as pwc
except Exception:  # pragma: no cover
    pwc = None  # type: ignore


# ===== Cross-platform Desktop command handlers =====


class GenericDesktopHandler(BaseDesktopHandler):
    """
    Generic desktop handler providing desktop-related operations.

    Implements:
    - get_desktop_environment: detect current desktop environment
    - set_wallpaper: set desktop wallpaper path
    """

    async def get_desktop_environment(self) -> Dict[str, Any]:
        import platform

        import screeninfo

        monitors = []
        try:
            for m in screeninfo.get_monitors():
                monitors.append({
                    "name": m.name,
                    "width": m.width,
                    "height": m.height,
                    "x": m.x,
                    "y": m.y,
                    "is_primary": m.is_primary if hasattr(m, "is_primary") else (m.x == 0 and m.y == 0),
                })
        except Exception:
            pass

        virtual_screen = None
        if IS_WINDOWS_PLATFORM:
            virtual_screen = get_virtual_screen_geometry().to_dict()

        environment = {
            "success": True,
            "environment": platform.system().lower(),
            "os_name": platform.system(),
            "os_version": platform.version(),
            "os_release": platform.release(),
            "machine": platform.machine(),
            "monitors": monitors,
        }
        if virtual_screen is not None:
            environment["virtual_screen"] = virtual_screen
        return environment

    async def set_wallpaper(self, path: str) -> Dict[str, Any]:
        try:
            import ctypes
            import winreg

            file_path = str(resolve_path(path))

            # Read previous wallpaper path before changing
            previous_wallpaper: Optional[str] = None
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop")
                previous_wallpaper, _ = winreg.QueryValueEx(key, "Wallpaper")
                winreg.CloseKey(key)
            except Exception:
                pass

            SPI_SETDESKWALLPAPER = 20
            ctypes.windll.user32.SystemParametersInfoW(
                SPI_SETDESKWALLPAPER, 0, file_path, 0
            )
            result: Dict[str, Any] = {"success": True}
            if previous_wallpaper is not None:
                result["previous_wallpaper"] = previous_wallpaper
            return result
        except Exception as e:
            return {"success": False, "error": str(e)}


# ===== Cross-platform window control command handlers =====


class GenericWindowHandler(BaseWindowHandler):
    """
    Cross-platform window management using pywinctl where possible.
    """

    async def open(self, target: str, browser: str = "") -> Dict[str, Any]:
        try:
            if target.startswith("http://") or target.startswith("https://"):
                if browser:
                    return await self._open_url_with_browser(target, browser)
                webbrowser.open(target)
                return {"success": True}
            # Resolve: absolute → PATH lookup → relative to CWD
            if os.path.isabs(target) and os.path.exists(target):
                path = target
            else:
                import shutil
                which = shutil.which(target)
                path = which if which else str(resolve_path(target))
            RuntimePermissionPolicy.from_env().require_local_open(path)
            os.startfile(path)  # type: ignore[attr-defined]
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _open_url_with_browser(self, url: str, browser: str) -> Dict[str, Any]:
        """Try to open a URL with a specific browser, falling back to default."""
        import shutil
        browser_exes = {
            "chrome": ["chrome", "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe"],
            "edge": ["msedge", "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe", "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe"],
            "firefox": ["firefox", "C:\\Program Files\\Mozilla Firefox\\firefox.exe"],
        }
        candidates = browser_exes.get(browser.lower(), [browser])
        for name in candidates:
            exe = shutil.which(name)
            if exe and os.path.exists(exe):
                subprocess.Popen([exe, url])
                return {"success": True, "browser": exe}
            if os.path.isabs(name) and os.path.exists(name):
                subprocess.Popen([name, url])
                return {"success": True, "browser": name}
        # Fallback
        webbrowser.open(url)
        return {"success": True, "browser": "default"}

    async def launch(self, app: str, args: Optional[list[str]] = None) -> Dict[str, Any]:
        """Launch an application by name, path, or alias.

        On Windows, uses four-pass resolution:
          1. PATH lookup — shutil.which (fastest, ~1ms)
          2. Registry Uninstall — DisplayName match (covers all installed apps)
          3. shell:AppsFolder — unified app registry (desktop + UWP)
          4. Start Menu scan — .lnk shortcut name match
        """
        if platform.system().lower() == "windows":
            return await self._launch_windows(app, args)
        return {"success": False, "error": f"Unsupported OS for application launching"}

    async def _launch_windows(
        self, app: str, args: Optional[list[str]] = None
    ) -> Dict[str, Any]:
        """Windows app resolution (Win32 + UWP)."""
        import shutil

        # Pass 1 — PATH / absolute path (fastest)
        resolved = shutil.which(app) or shutil.which(app + ".exe")

        # Pass 2 — Registry Uninstall (canonical source for "设置→应用")
        if not resolved:
            resolved = self._find_in_registry_uninstall(app)

        # Pass 3 — shell:AppsFolder (Win32 + UWP, returns file path or AUMID)
        if not resolved:
            resolved = self._find_in_apps_folder(app)

        # Pass 4 — Start Menu .lnk scan
        if not resolved:
            resolved = self._find_in_start_menu(app)

        return await GenericWindowHandler._resolve_and_launch(resolved, args)

    @staticmethod
    def _find_in_registry_uninstall(app: str) -> Optional[str]:
        """Search Windows Uninstall registry for name match.

        This is the same data source Windows uses for 设置→应用→安装的应用.
        Matches against both DisplayName and registry subkey name.
        """
        try:
            import winreg
        except ImportError:
            return None

        try:
            needle = app.lower()
            candidates: list[tuple[str, str]] = []  # (name, exe_path)

            def _matches(subkey_name: str, display_name: str) -> bool:
                for name in (subkey_name.lower(), display_name.lower()):
                    if needle == name:
                        return True
                    if needle in name or name in needle:
                        return True
                return False

            def _scan(key_path: int, root: int) -> bool:
                """Scan one hive. Returns True once a result is available."""
                try:
                    with winreg.OpenKey(root, key_path) as uninstall:
                        idx = 0
                        while True:
                            try:
                                subkey_name = winreg.EnumKey(uninstall, idx)
                                idx += 1
                                with winreg.OpenKey(uninstall, subkey_name) as subkey:
                                    try:
                                        display_name, _ = winreg.QueryValueEx(subkey, "DisplayName")
                                        if not isinstance(display_name, str) or not display_name.strip():
                                            display_name = subkey_name
                                    except OSError:
                                        display_name = subkey_name
                                    if not _matches(subkey_name, display_name):
                                        continue
                                    path = GenericWindowHandler._extract_exe_from_uninstall_key(subkey, app, subkey_name)
                                    if path:
                                        candidates.append((display_name, path))
                                        return True
                            except OSError:
                                break
                except OSError:
                    pass
                return False

            # 64-bit apps → 32-bit apps → per-user, stop after first match
            for key_path, root in [
                (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", winreg.HKEY_LOCAL_MACHINE),
                (r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", winreg.HKEY_LOCAL_MACHINE),
                (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", winreg.HKEY_CURRENT_USER),
            ]:
                if _scan(key_path, root) and candidates:
                    return candidates[0][1]

            if candidates:
                return candidates[0][1]
        except Exception as e:
            logger.debug("Registry uninstall scan failed for '%s': %s", app, e)
        return None

    # Common Electron / multi-dir install patterns searched under hint_dir
    _EXE_SUBDIRS = [
        "",
        os.path.join("main", "current"),
        os.path.join("main", "current_new"),
        "bin",
    ]

    @staticmethod
    def _try_candidate_exes(dir_path: str, candidates: list[str]) -> Optional[str]:
        """Return the first existing {dir_path}/{name}.exe for any name in candidates."""
        for name in candidates:
            guessed = os.path.join(dir_path, f"{name}.exe")
            if os.path.isfile(guessed):
                return guessed
        return None

    @staticmethod
    def _extract_exe_from_uninstall_key(subkey, app: str, subkey_name: str = "") -> Optional[str]:
        """Extract the main executable path from an Uninstall registry key.

        Tries candidate exe names: app name first, then subkey_name (which
        is often the English name when DisplayName is localised, e.g.
        app="钉钉" + subkey_name="DingTalk" → tries "钉钉.exe" then "DingTalk.exe").
        """
        import glob as _glob
        import winreg

        candidates = [app]
        if subkey_name and subkey_name.lower() != app.lower():
            candidates.append(subkey_name)

        hint_dir: Optional[str] = None
        _try = GenericWindowHandler._try_candidate_exes

        # Priority 1: InstallLocation
        try:
            location, _ = winreg.QueryValueEx(subkey, "InstallLocation")
            if isinstance(location, str) and os.path.isdir(location):
                hint_dir = location
                result = _try(location, candidates)
                if result:
                    return result
        except OSError:
            pass

        # Priority 2: UninstallString
        try:
            uninstall_str, _ = winreg.QueryValueEx(subkey, "UninstallString")
            if isinstance(uninstall_str, str):
                u_dir = os.path.dirname(uninstall_str.strip('"').split(",")[0])
                if not hint_dir and os.path.isdir(u_dir):
                    hint_dir = u_dir
                result = _try(u_dir, candidates)
                if result:
                    return result
        except OSError:
            pass

        # Priority 3: DisplayIcon
        try:
            icon, _ = winreg.QueryValueEx(subkey, "DisplayIcon")
            if isinstance(icon, str):
                path = icon.strip('"').rstrip(",").split(",")[0]
                if not hint_dir and os.path.isdir(os.path.dirname(path)):
                    hint_dir = os.path.dirname(path)
                if path.lower().endswith(".exe") and os.path.isfile(path):
                    return path
        except OSError:
            pass

        # Priority 4: search hint_dir subdirs for candidate.exe
        if hint_dir:
            for name in candidates:
                for sub in GenericWindowHandler._EXE_SUBDIRS:
                    p = os.path.join(hint_dir, sub, f"{name}.exe")
                    if os.path.isfile(p):
                        return p
            # Last resort: first .exe in hint_dir
            for w in _glob.glob(os.path.join(hint_dir, "*.exe")):
                return w

        return None

    @staticmethod
    def _is_file_path(path: str) -> bool:
        """Check if a path string looks like a launchable executable file."""
        return bool(path and os.path.isabs(path) and os.path.isfile(path))

    @staticmethod
    def _is_aumid(path: str) -> bool:
        """Check if a string looks like an AppUserModelId (e.g. 'Publisher.App!AppId')."""
        return bool(path and "!" in path and not os.path.isabs(path))

    @staticmethod
    def _find_in_apps_folder(app: str) -> Optional[str]:
        """Search shell:AppsFolder with exact match first, then fuzzy fallback.

        Returns either a file path (for Win32 apps) or an AppUserModelId
        (for UWP apps).  File paths take priority over AUMIDs.
        """
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            shell = win32com.client.Dispatch("Shell.Application")
            apps_folder = shell.NameSpace("shell:AppsFolder")
            if apps_folder is None:
                return None
            needle = app.lower()
            exact_aumid: Optional[str] = None
            fuzzy_file: Optional[str] = None
            fuzzy_aumid: Optional[str] = None
            items = apps_folder.Items()
            for item in items:
                name = str(item.Name or "")
                name_lower = name.lower()
                path = str(item.Path or "")
                if not path:
                    continue

                is_file = GenericWindowHandler._is_file_path(path)
                is_uwp = GenericWindowHandler._is_aumid(path)
                if not is_file and not is_uwp:
                    continue

                # Match against both display name and Path/AUMID
                path_lower = path.lower()
                matched = needle == name_lower or needle == path_lower
                fuzzy_matched = (
                    needle in name_lower or name_lower in needle
                    or needle in path_lower or path_lower in needle
                )

                if matched:
                    if is_file:
                        return path  # best: exact file
                    if exact_aumid is None:
                        exact_aumid = path
                elif fuzzy_matched:
                    if is_file and fuzzy_file is None:
                        fuzzy_file = path
                    elif is_uwp and fuzzy_aumid is None:
                        fuzzy_aumid = path

            return exact_aumid or fuzzy_file or fuzzy_aumid
        except Exception as e:
            logger.debug("AppsFolder scan failed for '%s': %s", app, e)
        return None

    @staticmethod
    def _activate_uwp_app(aumid: str) -> Optional[int]:
        """Launch a UWP app via IApplicationActivationManager::ActivateApplication.

        Uses raw ctypes COM vtable — no extra dependencies.
        Returns the PID on success, None on failure.
        """
        import ctypes
        from ctypes import byref, c_void_p, POINTER, cast, WINFUNCTYPE

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        CLSID = _GUID(
            0x45BA127D, 0x10A8, 0x46EA,
            (0x8A, 0xB7, 0x56, 0xEA, 0x90, 0x78, 0x94, 0x3C),
        )
        IID = _GUID(
            0x2E941141, 0x7F97, 0x4756,
            (0xBA, 0x1D, 0x9D, 0xEC, 0xDE, 0x89, 0x4A, 0x3D),
        )

        CLSCTX_LOCAL_SERVER = 4
        VTBL_ACTIVATE_APP = 3
        VTBL_RELEASE = 2
        AO_NONE = 0

        ole32 = ctypes.WinDLL("ole32.dll")
        obj = c_void_p()
        hr = ole32.CoCreateInstance(
            byref(CLSID), None, CLSCTX_LOCAL_SERVER,
            byref(IID), byref(obj),
        )
        if hr != 0 or not obj:
            return None

        vtable = cast(obj, POINTER(c_void_p))[0]
        vtbl = cast(vtable, POINTER(c_void_p))

        proto = WINFUNCTYPE(
            ctypes.c_long, c_void_p,
            ctypes.c_wchar_p, ctypes.c_wchar_p,
            ctypes.c_ulong, POINTER(ctypes.c_ulong),
        )
        activate = proto(vtbl[VTBL_ACTIVATE_APP])

        pid = ctypes.c_ulong(0)
        hr = activate(obj, aumid, "", AO_NONE, byref(pid))

        release_fn = WINFUNCTYPE(ctypes.c_ulong, c_void_p)(vtbl[VTBL_RELEASE])
        release_fn(obj)

        if hr == 0:
            return pid.value
        return None

    @staticmethod
    def _find_and_activate_existing(exe_path: str) -> Optional[Dict[str, Any]]:
        """If the app is already running, activate its window instead of launching."""
        exe_name = os.path.basename(exe_path)

        # Check if process is running via tasklist
        try:
            result = subprocess.run(
                ["tasklist", "/fi", f"IMAGENAME eq {exe_name}", "/fo", "csv", "/nh"],
                capture_output=True, text=True, timeout=5,
            )
        except Exception:
            return None

        pids: set[int] = set()
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("INFO:"):
                continue
            parts = line.replace('"', "").split(",")
            if len(parts) >= 2 and parts[0].strip().lower() == exe_name.lower():
                try:
                    pids.add(int(parts[1].strip()))
                except ValueError:
                    pass

        if not pids:
            return None

        # Find and activate a visible window belonging to this process
        try:
            import win32gui
            import win32process
        except Exception:
            return None

        target: list[tuple[int, str]] = []

        def _cb(hwnd: int, _ctx: None) -> bool:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid in pids:
                    title = win32gui.GetWindowText(hwnd) or ""
                    target.append((hwnd, title))
            except Exception:
                pass
            return True

        win32gui.EnumWindows(_cb, None)

        if target:
            # Prefer windows with non-empty titles (main windows)
            target.sort(key=lambda t: (0 if t[1] else 1))
            hwnd, title = target[0]
            try:
                win32gui.SetForegroundWindow(hwnd)
                return {
                    "success": True,
                    "reused": True,
                    "pid": min(pids),
                    "resolved_path": exe_path,
                    "window_title": title,
                }
            except Exception:
                pass

        return None

    @staticmethod
    async def _resolve_and_launch(resolved: Optional[str], args: Optional[list[str]]) -> Dict[str, Any]:
        """Launch a resolved app path — handles file paths, UWP AUMIDs, and reuse."""
        if not resolved:
            return {
                "success": False,
                "error": "Could not locate app",
                "suggestion": (
                    "Provide an absolute path (e.g. 'C:\\Program Files\\App\\app.exe'), "
                    "or a registered name (e.g. 'chrome', 'firefox', 'notepad')"
                ),
            }

        if os.path.isfile(resolved):
            # Reuse existing instance if already running
            existing = GenericWindowHandler._find_and_activate_existing(resolved)
            if existing:
                return existing
            cmd = [resolved] + (list(args) if args else [])
            proc = subprocess.Popen(cmd)
            return {"success": True, "pid": proc.pid, "resolved_path": resolved}

        if GenericWindowHandler._is_aumid(resolved):
            pid = GenericWindowHandler._activate_uwp_app(resolved)
            if pid:
                return {"success": True, "pid": pid, "resolved_path": resolved, "launch_method": "uwp"}
            return {"success": False, "error": f"UWP activation failed for '{resolved}'"}

        return {"success": False, "error": f"Unrecognized app reference: '{resolved}'"}

    @staticmethod
    def _find_in_start_menu(app: str) -> Optional[str]:
        """Scan Start Menu .lnk files for a case-insensitive name match."""
        needle = app.lower()
        roots = [
            os.path.join(
                os.environ.get("PROGRAMDATA", ""),
                "Microsoft", "Windows", "Start Menu", "Programs",
            ),
            os.path.join(
                os.environ.get("APPDATA", ""),
                "Microsoft", "Windows", "Start Menu", "Programs",
            ),
        ]
        for root in roots:
            if not os.path.isdir(root):
                continue
            for dirpath, _dirnames, filenames in os.walk(root):
                for fname in filenames:
                    if not fname.lower().endswith(".lnk"):
                        continue
                    if fname.lower()[:-4] == needle:
                        target = GenericWindowHandler._resolve_shortcut(
                            os.path.join(dirpath, fname)
                        )
                        if target and os.path.isfile(target):
                            return target
        return None

    @staticmethod
    def _resolve_shortcut(lnk_path: str) -> Optional[str]:
        """Resolve a .lnk shortcut to its target path."""
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            shell = win32com.client.Dispatch("WScript.Shell")
            return shell.CreateShortCut(lnk_path).TargetPath
        except Exception:
            return None

    def _get_window_by_id(self, window_id: int | str) -> Optional[Any]:
        if pwc is None:
            raise RuntimeError("pywinctl not available")
        # Find by native handle among Window objects; getAllWindowsDict keys are titles
        try:
            for w in pwc.getAllWindows():
                if str(w.getHandle()) == str(window_id):
                    return w
            return None
        except Exception:
            return None

    async def get_current_window_id(self) -> Dict[str, Any]:
        try:
            if pwc is None:
                return {"success": False, "error": "pywinctl not available"}
            win = pwc.getActiveWindow()
            if not win:
                return {"success": False, "error": "No active window"}
            return {"success": True, "window_id": win.getHandle(), "name": win.title}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_application_windows(self, app: str) -> Dict[str, Any]:
        try:
            if pwc is None:
                return {"success": False, "error": "pywinctl not available"}
            all_wins = pwc.getAllWindows()

            # Smart dispatch: numeric → PID, string → title + process name
            try:
                pid = int(app)
                all_wins = [w for w in all_wins if w.getPID() == pid]
            except ValueError:
                a = app.lower()
                import psutil
                matched = []
                for w in all_wins:
                    title = (w.title or "").lower()
                    # Match by title
                    if a in title:
                        matched.append(w)
                        continue
                    # Match by process name — skip legacy/background windows
                    try:
                        p = psutil.Process(w.getPID())
                        pname = (p.name() or "").lower()
                        if a in pname:
                            # Real windows have non-empty titles
                            if title:
                                matched.append(w)
                    except Exception:
                        pass
                all_wins = matched

            windows = []
            for w in all_wins:
                hwnd = w.getHandle()
                if not _is_meaningful_window(hwnd):
                    continue
                windows.append({"id": hwnd, "window_id": hwnd, "title": w.title or ""})
            return {"success": True, "windows": windows}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_window_name(self, window_id: int | str) -> Dict[str, Any]:
        try:
            if pwc is None:
                return {"success": False, "error": "pywinctl not available"}
            w = self._get_window_by_id(window_id)
            if not w:
                return {"success": False, "error": "Window not found"}
            return {"success": True, "name": w.title}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_window_size(self, window_id: int | str) -> Dict[str, Any]:
        try:
            if pwc is None:
                return {"success": False, "error": "pywinctl not available"}
            w = self._get_window_by_id(window_id)
            if not w:
                return {"success": False, "error": "Window not found"}
            width, height = w.size
            return {"success": True, "width": int(width), "height": int(height)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_window_position(self, window_id: int | str) -> Dict[str, Any]:
        try:
            if pwc is None:
                return {"success": False, "error": "pywinctl not available"}
            w = self._get_window_by_id(window_id)
            if not w:
                return {"success": False, "error": "Window not found"}
            x, y = w.position
            return {"success": True, "x": int(x), "y": int(y)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def set_window_size(
        self, window_id: int | str, width: int, height: int
    ) -> Dict[str, Any]:
        try:
            if pwc is None:
                return {"success": False, "error": "pywinctl not available"}
            w = self._get_window_by_id(window_id)
            if not w:
                return {"success": False, "error": "Window not found"}
            ok = w.resizeTo(int(width), int(height))
            return {"success": bool(ok)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def set_window_position(self, window_id: int | str, x: int, y: int) -> Dict[str, Any]:
        try:
            if pwc is None:
                return {"success": False, "error": "pywinctl not available"}
            w = self._get_window_by_id(window_id)
            if not w:
                return {"success": False, "error": "Window not found"}
            ok = w.moveTo(int(x), int(y))
            return {"success": bool(ok)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def maximize_window(self, window_id: int | str) -> Dict[str, Any]:
        try:
            if pwc is None:
                return {"success": False, "error": "pywinctl not available"}
            w = self._get_window_by_id(window_id)
            if not w:
                return {"success": False, "error": "Window not found"}
            ok = w.maximize()
            return {"success": bool(ok)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def minimize_window(self, window_id: int | str) -> Dict[str, Any]:
        try:
            if pwc is None:
                return {"success": False, "error": "pywinctl not available"}
            w = self._get_window_by_id(window_id)
            if not w:
                return {"success": False, "error": "Window not found"}
            ok = w.minimize()
            return {"success": bool(ok)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def activate_window(self, window_id: int | str) -> Dict[str, Any]:
        try:
            if pwc is None:
                return {"success": False, "error": "pywinctl not available"}
            w = self._get_window_by_id(window_id)
            if not w:
                return {"success": False, "error": "Window not found"}

            w.activate()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def close_window(self, window_id: int | str) -> Dict[str, Any]:
        try:
            if pwc is None:
                return {"success": False, "error": "pywinctl not available"}
            w = self._get_window_by_id(window_id)
            if not w:
                return {"success": False, "error": "Window not found"}
            try:
                w.close()
            except Exception:
                pass
            # Verify: pywinctl's close() often returns falsy even on success
            import asyncio
            await asyncio.sleep(0.1)
            still_exists = self._get_window_by_id(window_id) is not None
            return {"success": not still_exists}
        except Exception as e:
            return {"success": False, "error": str(e)}


# ===== Cross-platform file system command handlers =====


class GenericFileHandler(BaseFileHandler):
    """
    Generic file handler that provides file system operations for all operating systems.

    This class implements the BaseFileHandler interface and provides methods for
    file and directory operations including reading, writing, creating, and deleting
    files and directories.
    """

    async def file_exists(self, path: str) -> Dict[str, Any]:
        """
        Check if a file exists at the specified path.

        Args:
            path: The file path to check

        Returns:
            Dict containing 'success' boolean and either 'exists' boolean or 'error' string
        """
        try:
            p = resolve_allowed_path(path, "file_exists")
            return {"success": True, "exists": p.is_file(), **_audit_path(p, "file_exists")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def directory_exists(self, path: str) -> Dict[str, Any]:
        """
        Check if a directory exists at the specified path.

        Args:
            path: The directory path to check

        Returns:
            Dict containing 'success' boolean and either 'exists' boolean or 'error' string
        """
        try:
            p = resolve_allowed_path(path, "directory_exists")
            return {"success": True, "exists": p.is_dir(), **_audit_path(p, "directory_exists")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def list_dir(self, path: str) -> Dict[str, Any]:
        """
        List all files and directories in the specified directory.

        Args:
            path: The directory path to list

        Returns:
            Dict containing 'success' boolean and either 'files' list of names or 'error' string
        """
        try:
            p = resolve_allowed_path(path, "list_dir")
            return {
                "success": True,
                "files": [
                    child.name for child in p.iterdir() if child.is_file() or child.is_dir()
                ],
                **_audit_path(p, "list_dir"),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def read_text(self, path: str) -> Dict[str, Any]:
        try:
            p = resolve_allowed_path(path, "read_text")
            for enc in ("utf-8", "utf-16", "gbk", "latin-1"):
                try:
                    return {"success": True, "content": p.read_text(encoding=enc), **_audit_path(p, "read_text")}
                except (UnicodeDecodeError, UnicodeError):
                    continue
            return {"success": False, "error": f"Could not decode file: {path}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def write_text(self, path: str, content: str) -> Dict[str, Any]:
        """
        Write text content to a file.

        Args:
            path: The file path to write to
            content: The text content to write

        Returns:
            Dict containing 'success' boolean and optionally 'error' string
        """
        try:
            p = resolve_allowed_path(path, "write_text", dangerous=True)
            p.write_text(content)
            return {"success": True, **_audit_path(p, "write_text")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def write_bytes(
        self, path: str, content_b64: str, append: bool = False
    ) -> Dict[str, Any]:
        """
        Write binary content to a file from base64 encoded string.

        Args:
            path: The file path to write to
            content_b64: Base64 encoded binary content
            append: If True, append to existing file; if False, overwrite

        Returns:
            Dict containing 'success' boolean and optionally 'error' string
        """
        try:
            p = resolve_allowed_path(path, "write_bytes", dangerous=True)
            mode = "ab" if append else "wb"
            with open(p, mode) as f:
                f.write(base64.b64decode(content_b64))
            return {"success": True, **_audit_path(p, "write_bytes")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def read_bytes(
        self, path: str, offset: int = 0, length: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Read binary content from a file and return as base64 encoded string.

        Args:
            path: The file path to read from
            offset: Byte offset to start reading from
            length: Number of bytes to read; if None, read entire file from offset

        Returns:
            Dict containing 'success' boolean and either 'content_b64' string or 'error' string
        """
        try:
            file_path = resolve_allowed_path(path, "read_bytes")
            with open(file_path, "rb") as f:
                if offset > 0:
                    f.seek(offset)

                if length is not None:
                    content = f.read(length)
                else:
                    content = f.read()

            return {"success": True, "content_b64": base64.b64encode(content).decode("utf-8"), **_audit_path(file_path, "read_bytes")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_file_size(self, path: str) -> Dict[str, Any]:
        """
        Get the size of a file in bytes.

        Args:
            path: The file path to get size for

        Returns:
            Dict containing 'success' boolean and either 'size' integer or 'error' string
        """
        try:
            file_path = resolve_allowed_path(path, "get_file_size")
            size = file_path.stat().st_size
            return {"success": True, "size": size, **_audit_path(file_path, "get_file_size")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def delete_file(self, path: str) -> Dict[str, Any]:
        """
        Delete a file at the specified path.

        Args:
            path: The file path to delete

        Returns:
            Dict containing 'success' boolean and optionally 'error' string
        """
        try:
            p = resolve_allowed_path(path, "delete_file", dangerous=True)
            p.unlink()
            return {"success": True, **_audit_path(p, "delete_file")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def create_dir(self, path: str) -> Dict[str, Any]:
        """
        Create a directory at the specified path.

        Creates parent directories if they don't exist and doesn't raise an error
        if the directory already exists.

        Args:
            path: The directory path to create

        Returns:
            Dict containing 'success' boolean and optionally 'error' string
        """
        try:
            p = resolve_allowed_path(path, "create_dir", dangerous=True)
            p.mkdir(parents=True, exist_ok=True)
            return {"success": True, **_audit_path(p, "create_dir")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def delete_dir(self, path: str) -> Dict[str, Any]:
        """
        Delete an empty directory at the specified path.

        Args:
            path: The directory path to delete

        Returns:
            Dict containing 'success' boolean and optionally 'error' string
        """
        try:
            p = resolve_allowed_path(path, "delete_dir", dangerous=True)
            p.rmdir()
            return {"success": True, **_audit_path(p, "delete_dir")}
        except Exception as e:
            return {"success": False, "error": str(e)}
