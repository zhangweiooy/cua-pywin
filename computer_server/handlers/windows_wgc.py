"""Windows.Graphics.Capture window capture backend."""

from __future__ import annotations

import asyncio
import ctypes
import os
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict


HRESULT = ctypes.c_long
UINT = ctypes.c_uint

D3D_DRIVER_TYPE_HARDWARE = 1
D3D_DRIVER_TYPE_WARP = 5
D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20
D3D11_SDK_VERSION = 7
D3D11_USAGE_STAGING = 3
D3D11_CPU_ACCESS_READ = 0x20000
D3D11_MAP_READ = 1

DXGI_FORMAT_B8G8R8A8_UNORM = 87

VTABLE_IUNKNOWN_QUERY_INTERFACE = 0
VTABLE_IUNKNOWN_RELEASE = 2
VTABLE_ID3D11DEVICE_CREATE_TEXTURE2D = 5
VTABLE_ID3D11TEXTURE2D_GET_DESC = 10
VTABLE_ID3D11DEVICECONTEXT_MAP = 14
VTABLE_ID3D11DEVICECONTEXT_UNMAP = 15
VTABLE_ID3D11DEVICECONTEXT_COPY_RESOURCE = 47

_FEATURE_LEVELS = (ctypes.c_uint * 4)(0xB100, 0xB000, 0xA100, 0xA000)


class WgcCaptureError(RuntimeError):
    """User-facing WGC capture failure."""


class WgcUnavailable(WgcCaptureError):
    """WGC is unavailable or disabled."""


@dataclass(frozen=True)
class WgcWindowCapture:
    image: Any
    metadata: Dict[str, Any]


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, value: str) -> None:
        guid = uuid.UUID(value)
        raw = guid.bytes_le
        super().__init__(
            int.from_bytes(raw[0:4], "little"),
            int.from_bytes(raw[4:6], "little"),
            int.from_bytes(raw[6:8], "little"),
            (ctypes.c_ubyte * 8).from_buffer_copy(raw[8:16]),
        )


IID_ID3D11Texture2D = _GUID("{6f15aaf2-d208-4e89-9ab4-489535d34f9c}")


class _DXGI_SAMPLE_DESC(ctypes.Structure):
    _fields_ = [("Count", UINT), ("Quality", UINT)]


class _D3D11_TEXTURE2D_DESC(ctypes.Structure):
    _fields_ = [
        ("Width", UINT),
        ("Height", UINT),
        ("MipLevels", UINT),
        ("ArraySize", UINT),
        ("Format", UINT),
        ("SampleDesc", _DXGI_SAMPLE_DESC),
        ("Usage", UINT),
        ("BindFlags", UINT),
        ("CPUAccessFlags", UINT),
        ("MiscFlags", UINT),
    ]


class _D3D11_MAPPED_SUBRESOURCE(ctypes.Structure):
    _fields_ = [
        ("pData", ctypes.c_void_p),
        ("RowPitch", UINT),
        ("DepthPitch", UINT),
    ]


def _vfunc(ptr: int, index: int, restype: Any, *argtypes: Any) -> Any:
    vtable = ctypes.cast(
        ptr,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
    ).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])


def _release(ptr: int | None) -> None:
    if not ptr:
        return
    try:
        _vfunc(ptr, VTABLE_IUNKNOWN_RELEASE, UINT)(ptr)
    except Exception:
        pass


def _query_interface(ptr: int, iid: _GUID) -> int:
    out = ctypes.c_void_p()
    hr = _vfunc(
        ptr,
        VTABLE_IUNKNOWN_QUERY_INTERFACE,
        HRESULT,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    )(ptr, ctypes.byref(iid), ctypes.byref(out))
    if hr < 0 or not out.value:
        raise OSError(hr, "QueryInterface failed")
    return int(out.value)


@dataclass
class _D3DDeviceBundle:
    device_ptr: int
    context_ptr: int
    winrt_device: Any
    driver_type: str
    feature_level: int

    def close(self) -> None:
        try:
            self.winrt_device.close()
        except Exception:
            pass
        _release(self.context_ptr)
        _release(self.device_ptr)


def _create_d3d_device() -> _D3DDeviceBundle:
    d3d11_create_device = ctypes.windll.d3d11.D3D11CreateDevice
    d3d11_create_device.restype = HRESULT

    from winrt.windows.graphics.directx.direct3d11.interop import (
        create_direct3d11_device_from_dxgi_device,
    )

    last_hr = 0
    for driver_type, driver_name in (
        (D3D_DRIVER_TYPE_HARDWARE, "hardware"),
        (D3D_DRIVER_TYPE_WARP, "warp"),
    ):
        device = ctypes.c_void_p()
        context = ctypes.c_void_p()
        feature_level = UINT()
        hr = d3d11_create_device(
            None,
            driver_type,
            None,
            D3D11_CREATE_DEVICE_BGRA_SUPPORT,
            _FEATURE_LEVELS,
            len(_FEATURE_LEVELS),
            D3D11_SDK_VERSION,
            ctypes.byref(device),
            ctypes.byref(feature_level),
            ctypes.byref(context),
        )
        last_hr = int(hr)
        if hr >= 0 and device.value and context.value:
            winrt_device = create_direct3d11_device_from_dxgi_device(int(device.value))
            return _D3DDeviceBundle(
                device_ptr=int(device.value),
                context_ptr=int(context.value),
                winrt_device=winrt_device,
                driver_type=driver_name,
                feature_level=int(feature_level.value),
            )

    raise WgcUnavailable(f"D3D11CreateDevice failed: HRESULT 0x{last_hr & 0xFFFFFFFF:08x}")


def _texture_to_pil_image(
    device_ptr: int,
    context_ptr: int,
    surface: Any,
) -> tuple[Any, Dict[str, Any]]:
    from PIL import Image
    from winrt.windows.graphics.directx.direct3d11.interop import (
        get_dxgi_surface_from_object,
    )

    dxgi_surface = int(get_dxgi_surface_from_object(surface))
    texture_ptr: int | None = None
    staging = ctypes.c_void_p()
    mapped = _D3D11_MAPPED_SUBRESOURCE()

    try:
        texture_ptr = _query_interface(dxgi_surface, IID_ID3D11Texture2D)

        desc = _D3D11_TEXTURE2D_DESC()
        _vfunc(
            texture_ptr,
            VTABLE_ID3D11TEXTURE2D_GET_DESC,
            None,
            ctypes.POINTER(_D3D11_TEXTURE2D_DESC),
        )(
            texture_ptr,
            ctypes.byref(desc),
        )
        if int(desc.Format) != DXGI_FORMAT_B8G8R8A8_UNORM:
            raise WgcCaptureError(f"unsupported WGC pixel format: {int(desc.Format)}")

        staging_desc = _D3D11_TEXTURE2D_DESC(
            desc.Width,
            desc.Height,
            1,
            1,
            desc.Format,
            _DXGI_SAMPLE_DESC(1, 0),
            D3D11_USAGE_STAGING,
            0,
            D3D11_CPU_ACCESS_READ,
            0,
        )
        hr = _vfunc(
            device_ptr,
            VTABLE_ID3D11DEVICE_CREATE_TEXTURE2D,
            HRESULT,
            ctypes.POINTER(_D3D11_TEXTURE2D_DESC),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )(
            device_ptr,
            ctypes.byref(staging_desc),
            None,
            ctypes.byref(staging),
        )
        if hr < 0 or not staging.value:
            raise OSError(hr, "CreateTexture2D staging failed")

        _vfunc(
            context_ptr,
            VTABLE_ID3D11DEVICECONTEXT_COPY_RESOURCE,
            None,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )(
            context_ptr,
            int(staging.value),
            texture_ptr,
        )
        hr = _vfunc(
            context_ptr,
            VTABLE_ID3D11DEVICECONTEXT_MAP,
            HRESULT,
            ctypes.c_void_p,
            UINT,
            UINT,
            UINT,
            ctypes.POINTER(_D3D11_MAPPED_SUBRESOURCE),
        )(
            context_ptr,
            int(staging.value),
            0,
            D3D11_MAP_READ,
            0,
            ctypes.byref(mapped),
        )
        if hr < 0 or not mapped.pData:
            raise OSError(hr, "Map staging texture failed")

        width = int(desc.Width)
        height = int(desc.Height)
        rows = [
            ctypes.string_at(int(mapped.pData) + y * int(mapped.RowPitch), width * 4)
            for y in range(height)
        ]
        data = b"".join(rows)
        _vfunc(context_ptr, VTABLE_ID3D11DEVICECONTEXT_UNMAP, None, ctypes.c_void_p, UINT)(
            context_ptr,
            int(staging.value),
            0,
        )
        mapped.pData = None

        image = Image.frombuffer("RGBA", (width, height), data, "raw", "BGRA", 0, 1).copy()
        return image, {
            "texture_width": width,
            "texture_height": height,
            "dxgi_format": int(desc.Format),
            "row_pitch": int(mapped.RowPitch),
        }
    finally:
        if mapped.pData and staging.value:
            try:
                _vfunc(
                    context_ptr,
                    VTABLE_ID3D11DEVICECONTEXT_UNMAP,
                    None,
                    ctypes.c_void_p,
                    UINT,
                )(
                    context_ptr,
                    int(staging.value),
                    0,
                )
            except Exception:
                pass
        _release(int(staging.value) if staging.value else None)
        _release(texture_ptr)
        _release(dxgi_surface)


def is_available() -> bool:
    if sys.platform != "win32":
        return False
    if os.environ.get("CUA_DISABLE_WGC") == "1":
        return False
    try:
        from winrt.windows.graphics.capture import GraphicsCaptureSession

        return bool(GraphicsCaptureSession.is_supported())
    except Exception:
        return False


async def capture_window_with_wgc(
    window_id: int,
    *,
    timeout_s: float = 1.0,
) -> WgcWindowCapture:
    """Capture one HWND using Windows.Graphics.Capture."""
    if sys.platform != "win32":
        raise WgcUnavailable("WGC is only available on Windows")
    if os.environ.get("CUA_DISABLE_WGC") == "1":
        raise WgcUnavailable("WGC disabled by CUA_DISABLE_WGC=1")

    import win32gui
    from winrt.windows.graphics.capture import (
        Direct3D11CaptureFramePool,
        GraphicsCaptureSession,
    )
    from winrt.windows.graphics.capture.interop import create_for_window
    from winrt.windows.graphics.directx import DirectXPixelFormat

    hwnd = int(window_id)
    if not win32gui.IsWindow(hwnd):
        raise WgcCaptureError(f"invalid window_id for WGC: {hwnd}")
    if win32gui.IsIconic(hwnd):
        raise WgcCaptureError("target_window_minimized")
    if not GraphicsCaptureSession.is_supported():
        raise WgcUnavailable("Windows.Graphics.Capture is not supported on this system")

    started = time.monotonic()
    item_started = time.monotonic()
    item = create_for_window(hwnd)
    if item is None:
        raise WgcCaptureError("WGC create_for_window returned None")
    if item.size.width <= 0 or item.size.height <= 0:
        raise WgcCaptureError(
            f"WGC item has invalid size: {item.size.width}x{item.size.height}"
        )
    item_latency = time.monotonic() - item_started

    d3d_started = time.monotonic()
    d3d = _create_d3d_device()
    d3d_latency = time.monotonic() - d3d_started
    frame_pool = None
    session = None
    frame = None
    try:
        setup_started = time.monotonic()
        frame_pool = Direct3D11CaptureFramePool.create_free_threaded(
            d3d.winrt_device,
            DirectXPixelFormat.B8_G8_R8_A8_UINT_NORMALIZED,
            1,
            item.size,
        )
        session = frame_pool.create_capture_session(item)
        try:
            session.is_cursor_capture_enabled = False
        except Exception:
            pass
        try:
            session.is_border_required = False
        except Exception:
            pass
        session.start_capture()
        session_setup_latency = time.monotonic() - setup_started

        frame_wait_started = time.monotonic()
        deadline = time.monotonic() + max(0.1, timeout_s)
        while time.monotonic() < deadline:
            frame = frame_pool.try_get_next_frame()
            if frame is not None:
                break
            await asyncio.sleep(0.02)
        if frame is None:
            raise WgcCaptureError(f"WGC frame timeout after {timeout_s:.2f}s")
        frame_wait_latency = time.monotonic() - frame_wait_started

        readback_started = time.monotonic()
        image, texture_metadata = _texture_to_pil_image(
            d3d.device_ptr,
            d3d.context_ptr,
            frame.surface,
        )
        texture_readback_latency = time.monotonic() - readback_started
        content_size = frame.content_size
        total_latency = time.monotonic() - started
        latency_ms = round(total_latency * 1000.0, 2)
        metadata = {
            "capture_backend": "wgc",
            "capture_source": "windows_graphics_capture",
            "capture_mode": "wgc_window",
            "occlusion_safe": True,
            "occlusion_risk": False,
            "item_display_name": item.display_name,
            "item_size": {
                "width": int(item.size.width),
                "height": int(item.size.height),
            },
            "content_size": {
                "width": int(content_size.width),
                "height": int(content_size.height),
            },
            "d3d_driver_type": d3d.driver_type,
            "d3d_feature_level": f"0x{d3d.feature_level:x}",
            "latency_ms": latency_ms,
            "stage_timings": {
                "wgc_item_latency": item_latency,
                "wgc_d3d_latency": d3d_latency,
                "wgc_session_setup_latency": session_setup_latency,
                "wgc_frame_wait_latency": frame_wait_latency,
                "wgc_texture_readback_latency": texture_readback_latency,
                "wgc_total_latency": total_latency,
            },
            **texture_metadata,
        }
        return WgcWindowCapture(image=image, metadata=metadata)
    finally:
        if frame is not None:
            try:
                frame.close()
            except Exception:
                pass
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        if frame_pool is not None:
            try:
                frame_pool.close()
            except Exception:
                pass
        d3d.close()
