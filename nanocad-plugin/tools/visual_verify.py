from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import time
import traceback
from ctypes import wintypes
from pathlib import Path

from PIL import Image


user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


class Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", BitmapInfoHeader), ("bmiColors", wintypes.DWORD * 3)]


EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
user32.EnumDesktopWindows.argtypes = [wintypes.HANDLE, EnumWindowsProc, wintypes.LPARAM]
user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
user32.GetThreadDesktop.restype = wintypes.HANDLE
user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
user32.OpenInputDesktop.restype = wintypes.HANDLE
user32.SwitchDesktop.argtypes = [wintypes.HANDLE]
user32.CloseDesktop.argtypes = [wintypes.HANDLE]
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(Rect)]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.ULONG]
user32.WindowFromPoint.argtypes = [Point]
user32.WindowFromPoint.restype = wintypes.HWND
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetWindowDC.argtypes = [wintypes.HWND]
user32.GetWindowDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.GetDIBits.argtypes = [
    wintypes.HDC,
    wintypes.HBITMAP,
    wintypes.UINT,
    wintypes.UINT,
    wintypes.LPVOID,
    ctypes.POINTER(BitmapInfo),
    wintypes.UINT,
]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]


def window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


def class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def enumerate_windows() -> list[dict[str, object]]:
    result: list[dict[str, object]] = []

    @EnumWindowsProc
    def callback(hwnd: int, _: int) -> bool:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        result.append(
            {
                "handle": int(hwnd),
                "pid": int(pid.value),
                "title": window_text(hwnd),
                "class": class_name(hwnd),
                "visible": bool(user32.IsWindowVisible(hwnd)),
            }
        )
        return True

    desktop = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
    if not desktop:
        raise ctypes.WinError(ctypes.get_last_error())
    ctypes.set_last_error(0)
    if not user32.EnumDesktopWindows(desktop, callback, 0):
        error = ctypes.get_last_error()
        if error:
            raise ctypes.WinError(error)
    return result


def find_main_window(pid: int) -> dict[str, object] | None:
    candidates = [
        item
        for item in enumerate_windows()
        if item["pid"] == pid
        and item["visible"]
        and "nanoCAD" in str(item["title"])
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: len(str(item["title"])))


def capture_window(hwnd: int) -> tuple[Image.Image, Rect]:
    rect = Rect()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    width = max(1, rect.right - rect.left)
    height = max(1, rect.bottom - rect.top)
    window_dc = user32.GetWindowDC(hwnd)
    if not window_dc:
        raise ctypes.WinError(ctypes.get_last_error())
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    old_bitmap = gdi32.SelectObject(memory_dc, bitmap)
    try:
        user32.PrintWindow(hwnd, memory_dc, 2)
        info = BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(BitmapInfoHeader)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0
        buffer = ctypes.create_string_buffer(width * height * 4)
        rows = gdi32.GetDIBits(
            memory_dc,
            bitmap,
            0,
            height,
            buffer,
            ctypes.byref(info),
            0,
        )
        if rows != height:
            raise RuntimeError(f"GetDIBits returned {rows} of {height} rows")
        image = Image.frombuffer("RGB", (width, height), buffer, "raw", "BGRX", 0, 1).copy()
        return image, rect
    finally:
        gdi32.SelectObject(memory_dc, old_bitmap)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)


def main() -> int:
    if len(sys.argv) != 6:
        raise SystemExit(
            "usage: visual_verify.py NCAD_EXE PLUGIN_DLL DRAWING SCREENSHOT STATUS_JSON"
        )
    ncad_exe, plugin_dll, drawing, screenshot, status_json = (
        Path(value).resolve() for value in sys.argv[1:]
    )
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    status_json.parent.mkdir(parents=True, exist_ok=True)
    probe_file = status_json.with_name(status_json.stem + "_interaction.json")
    prep_file = status_json.with_name(status_json.stem + "_selection.json")
    probe_file.unlink(missing_ok=True)
    prep_file.unlink(missing_ok=True)
    child_environment = dict(**__import__("os").environ)
    child_environment["GREENAI_CONTEXT_PROBE_FILE"] = str(probe_file)
    child_environment["GREENAI_CONTEXT_VISUAL_PREP"] = "1"
    child_environment["GREENAI_CONTEXT_PREP_FILE"] = str(prep_file)
    process = subprocess.Popen(
        [str(ncad_exe), "-g", str(plugin_dll), str(drawing)],
        cwd=str(ncad_exe.parent),
        env=child_environment,
    )
    status: dict[str, object] = {"pid": process.pid}
    original_desktop = wintypes.HANDLE()
    switched_desktop = False
    try:
        deadline = time.monotonic() + 150
        stable_handle = 0
        stable_count = 0
        main_window: dict[str, object] | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"nanoCAD exited with code {process.returncode}")
            candidate = find_main_window(process.pid)
            if candidate and drawing.stem.casefold() in str(candidate["title"]).casefold():
                handle = int(candidate["handle"])
                if handle == stable_handle:
                    stable_count += 1
                else:
                    stable_handle = handle
                    stable_count = 0
                if stable_count >= 4:
                    main_window = candidate
                    break
            else:
                stable_count = 0
            time.sleep(0.5)
        if main_window is None:
            raise RuntimeError("stable nanoCAD drawing window did not appear")

        prep_deadline = time.monotonic() + 5
        while not prep_file.exists() and time.monotonic() < prep_deadline:
            time.sleep(0.1)
        if not prep_file.exists():
            raise RuntimeError("object-selection preparation did not complete")
        status["selection_probe"] = json.loads(prep_file.read_text(encoding="utf-8"))

        hwnd = int(main_window["handle"])
        rect = Rect()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise ctypes.WinError(ctypes.get_last_error())
        user32.ShowWindow(hwnd, 9)
        x = int(rect.left + 0.72 * (rect.right - rect.left))
        y = int(rect.top + 0.62 * (rect.bottom - rect.top))
        original_desktop = user32.OpenInputDesktop(0, False, 0x10000000)
        if not original_desktop:
            raise ctypes.WinError(ctypes.get_last_error())
        verification_desktop = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
        if not user32.SwitchDesktop(verification_desktop):
            raise ctypes.WinError(ctypes.get_last_error())
        switched_desktop = True
        time.sleep(0.35)
        user32.SwitchToThisWindow(hwnd, True)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        if not user32.SetCursorPos(x, y):
            raise ctypes.WinError(ctypes.get_last_error())
        target = int(user32.WindowFromPoint(Point(x, y))) or hwnd
        user32.mouse_event(0x0008, 0, 0, 0, 0)
        user32.mouse_event(0x0010, 0, 0, 0, 0)
        time.sleep(0.7)

        windows_after_click = enumerate_windows()
        menus = [item for item in windows_after_click if item["class"] == "#32768"]
        status.update(
            {
                "main_window": main_window,
                "click": [x, y],
                "context_target": target,
                "menu_windows": menus,
                "screenshot": str(screenshot),
            }
        )
        if not menus:
            raise RuntimeError("no native context-menu window appeared")
        root_menu_image, root_menu_rect = capture_window(int(menus[0]["handle"]))
        user32.SetCursorPos(root_menu_rect.left + 45, root_menu_rect.top + 15)
        time.sleep(0.9)
        expanded_menus = [
            item for item in enumerate_windows() if item["class"] == "#32768" and item["visible"]
        ]
        status["expanded_menu_windows"] = expanded_menus
        if len(expanded_menus) < 2:
            raise RuntimeError("GreenAI submenu did not open")
        main_image, main_rect = capture_window(hwnd)
        canvas = main_image
        for menu in expanded_menus:
            menu_image, menu_rect = capture_window(int(menu["handle"]))
            canvas.paste(
                menu_image,
                (menu_rect.left - main_rect.left, menu_rect.top - main_rect.top),
            )
        canvas.save(screenshot)
        submenu = next(
            item for item in expanded_menus if int(item["handle"]) != int(menus[0]["handle"])
        )
        _, submenu_rect = capture_window(int(submenu["handle"]))
        user32.SetCursorPos(submenu_rect.left + 70, submenu_rect.top + 15)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
        probe_deadline = time.monotonic() + 5
        while not probe_file.exists() and time.monotonic() < probe_deadline:
            time.sleep(0.1)
        if not probe_file.exists():
            raise RuntimeError("GreenAI submenu click did not invoke the plugin handler")
        status["interaction_probe"] = json.loads(probe_file.read_text(encoding="utf-8"))
        status["ok"] = True
        return 0
    except Exception as error:
        status.update(
            {
                "ok": False,
                "error": f"{type(error).__name__}: {error}",
                "traceback": traceback.format_exc(),
            }
        )
        return 1
    finally:
        if switched_desktop and original_desktop:
            user32.SwitchDesktop(original_desktop)
        if original_desktop:
            user32.CloseDesktop(original_desktop)
        status_json.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
