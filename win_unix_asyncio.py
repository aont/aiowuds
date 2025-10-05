# win_unix_asyncio.py
# Experimental: AF_UNIX-like sockets on Windows + asyncio (Proactor)
# Tested as an experimental wrapper based on user-provided sample.
"""Asyncio helpers that emulate Unix domain sockets on Windows.

This module exposes a small surface area that mirrors the standard
``asyncio.start_unix_server`` and ``asyncio.open_unix_connection`` helpers, but
internally falls back to a ctypes-based Winsock implementation when the native
``AF_UNIX`` support is unavailable.  It is primarily geared towards
experimental projects that wish to keep the same code path on Windows and
Unix-like systems.
"""

from __future__ import annotations

import asyncio
import socket
import ctypes
import ctypes.wintypes
import os
from typing import Awaitable, Callable, Optional, Tuple

__all__ = [
    "UNIX_PATH_MAX",
    "WindowsUnixServer",
    "ensure_wsa_started",
    "open_unix_connection",
    "start_unix_server",
]

__version__ = "0.1.0"

# --- minimal ctypes wrappings (based on user's sample) ---
UNIX_PATH_MAX = 108

class Sockaddr(ctypes.Structure):
    _fields_ = [
        ("sun_family", ctypes.wintypes.USHORT),
        ("sun_data", ctypes.wintypes.CHAR * UNIX_PATH_MAX),
    ]

class WSAData(ctypes.Structure):
    _fields_ = [
        ("wVersion", ctypes.wintypes.WORD),
        ("wHighVersion", ctypes.wintypes.WORD),
        ("szDescription", ctypes.c_char * 256),
        ("szSystemStatus", ctypes.c_char * 128),
        ("iMaxSockets", ctypes.wintypes.USHORT),
        ("iMaxUdpDg", ctypes.wintypes.USHORT),
        ("lpVendorInfo", ctypes.c_char_p),
    ]

ws2_32 = ctypes.windll.ws2_32

# prototypes used
ws2_32.WSAStartup.restype = ctypes.c_int
ws2_32.WSAStartup.argtypes = (ctypes.wintypes.WORD, ctypes.POINTER(WSAData))

ws2_32.WSACreateEvent.restype = ctypes.wintypes.HANDLE
ws2_32.WSACreateEvent.argtypes = ()

ws2_32.WSAResetEvent.restype = ctypes.wintypes.BOOL
ws2_32.WSAResetEvent.argtypes = (ctypes.wintypes.HANDLE, )

ws2_32.WSAEventSelect.restype = ctypes.c_int
ws2_32.WSAEventSelect.argtypes = (ctypes.wintypes.HANDLE, ctypes.wintypes.HANDLE, ctypes.c_long)

ws2_32.WSAWaitForMultipleEvents.restype = ctypes.wintypes.DWORD
ws2_32.WSAWaitForMultipleEvents.argtypes = (ctypes.wintypes.DWORD, ctypes.POINTER(ctypes.wintypes.HANDLE), ctypes.wintypes.BOOL, ctypes.wintypes.DWORD, ctypes.wintypes.BOOL)

ws2_32.bind.restype = ctypes.wintypes.INT
ws2_32.bind.argtypes = (ctypes.wintypes.HANDLE, ctypes.POINTER(Sockaddr), ctypes.c_int)

ws2_32.accept.restype = ctypes.c_int
ws2_32.accept.argtypes = (ctypes.wintypes.HANDLE, ctypes.POINTER(Sockaddr), ctypes.POINTER(ctypes.c_int))

ws2_32.connect.restype = ctypes.c_int
ws2_32.connect.argtypes = (ctypes.wintypes.HANDLE, ctypes.POINTER(Sockaddr), ctypes.c_int)

# c_uintptr = ctypes.POINTER(ctypes.c_uint)
# ctypes.c_void_p
ws2_32.socket.restype = ctypes.c_void_p  # SOCKET
ws2_32.socket.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_int)

ws2_32.closesocket.restype = ctypes.c_int
ws2_32.closesocket.argtypes = (ctypes.c_void_p,)

ws2_32.WSAGetLastError.restype = ctypes.c_int
ws2_32.WSAGetLastError.argtypes = ()

# constants
FD_READ   = 0x01
FD_WRITE  = 0x02
FD_ACCEPT = 0x08
WSA_INFINITE = 0xffffffff

# --- utility: ensure WSA is started once ---
_wsa_started = False
def ensure_wsa_started() -> None:
    global _wsa_started
    if _wsa_started:
        return
    data = WSAData()
    ret = ws2_32.WSAStartup(0x0202, ctypes.byref(data))
    if ret != 0:
        raise OSError(f"WSAStartup failed: {ret}")
    _wsa_started = True

# --- high-level API classes / functions ---
class WindowsUnixServer:
    """
    Experimental server that accepts AF_UNIX-like connections on Windows using WSAEventSelect
    and hands them off to asyncio StreamReader/StreamWriter using loop.connect_accepted_socket.
    """
    def __init__(self, path: str, loop: Optional[asyncio.AbstractEventLoop] = None):
        self.path = path
        self.loop = loop or asyncio.get_event_loop()
        self._sock: Optional[socket.socket] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        # ensure proactor exists (Windows default)
        if not hasattr(self.loop, "_proactor"):
            raise RuntimeError("This helper requires an asyncio ProactorEventLoop (Windows).")
        self._proactor = self.loop._proactor  # type: ignore

    async def start(self, client_connected_cb: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]):
        """
        Start listening and accept connections, invoking client_connected_cb(reader, writer)
        for each accepted connection. This call returns immediately after scheduling the accept loop.
        """
        ensure_wsa_started()
        # prepare socket path
        if os.path.exists(self.path):
            try:
                os.unlink(self.path)
            except Exception:
                pass

        # Create a Python socket. (We rely on the AF_UNIX value being available or monkeypatched by user.)
        fam = getattr(socket, "AF_UNIX", 1)  # fallback to 1 if not present
        serv = socket.socket(fam, socket.SOCK_STREAM)
        serv.setblocking(False)
        self._sock = serv

        # bind using ctypes to match user's strategy (avoid Python internals on Windows)
        sockaddr = Sockaddr()
        sockaddr.sun_family = fam
        enc = self.path.encode("utf-8")
        if len(enc) >= UNIX_PATH_MAX:
            raise ValueError("socket path too long")
        # sockaddr.sun_data[:len(enc)] = enc  # copy
        tmp = bytearray(sockaddr.sun_data)          # バッファを mutable に取得
        tmp[:len(enc)] = enc                        # 書き換え
        sockaddr.sun_data = bytes(tmp)              # 再代入（固定長に自動切詰め/拡張）
        sockaddr_len = ctypes.sizeof(sockaddr)
        # call winsock bind on fileno
        ret = ws2_32.bind(serv.fileno(), ctypes.byref(sockaddr), sockaddr_len)
        if ret != 0:
            # best-effort readable error
            raise OSError(f"bind failed: {ret}")

        serv.listen(5)
        self._running = True
        # create accept-loop task
        self._task = self.loop.create_task(self._accept_loop(client_connected_cb))
        return self

    async def _accept_loop(self, client_connected_cb):
        assert self._sock is not None
        serv = self._sock
        while self._running:
            # create WSA event for FD_ACCEPT
            ev = ws2_32.WSACreateEvent()
            if not ev:
                await asyncio.sleep(0.1)
                continue
            # ask Winsock to signal the event when accept is possible
            ws2_32.WSAEventSelect(serv.fileno(), ev, FD_ACCEPT)
            # wait for event via proactor
            try:
                await self._proactor.wait_for_handle(ev)
            except Exception as e:
                # proactor may raise on loop shutdown
                ws2_32.WSAResetEvent(ev)
                break
            # reset event
            try:
                ws2_32.WSAResetEvent(ev)
            except Exception:
                pass

            # accept via winsock accept (returns raw socket handle)
            client_addr = Sockaddr()
            client_addr_len = ctypes.c_int(ctypes.sizeof(client_addr))
            client_fileno = ws2_32.accept(serv.fileno(), ctypes.byref(client_addr), ctypes.byref(client_addr_len))
            if client_fileno == -1 or client_fileno == 0:
                # nothing to accept or error
                await asyncio.sleep(0)
                continue

            # wrap into Python socket
            try:
                client_sock = socket.socket(fileno=client_fileno)
                client_sock.setblocking(False)
            except Exception as exc:
                # close handle if we failed to wrap (best-effort)
                try:
                    ctypes.windll.kernel32.CloseHandle(ctypes.wintypes.HANDLE(client_fileno))
                except Exception:
                    pass
                continue

            # build StreamReader/Writer via StreamReaderProtocol + loop.connect_accepted_socket
            reader = asyncio.StreamReader(limit=2**16, loop=self.loop)
            protocol = asyncio.StreamReaderProtocol(reader)
            try:
                # connect_accepted_socket hands the socket to transport/protocol
                transport, _ = await self.loop.connect_accepted_socket(lambda: protocol, client_sock)
            except Exception:
                client_sock.close()
                continue
            writer = asyncio.StreamWriter(transport, protocol, reader, self.loop)
            # hand off to user's callback (don't await here to keep accepting)
            self.loop.create_task(client_connected_cb(reader, writer))

    async def close(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        # remove socket file if exists
        try:
            if os.path.exists(self.path):
                os.unlink(self.path)
        except Exception:
            pass

# --- client helper: connect and return (reader, writer) ---
async def open_unix_connection(path: str, loop: Optional[asyncio.AbstractEventLoop] = None) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """
    Try native AF_UNIX first; if not available, create a winsock socket
    and connect via ctypes, then wrap into a Python socket and hand to asyncio.
    """
    loop = loop or asyncio.get_event_loop()
    ensure_wsa_started()

    # 1) ネイティブ AF_UNIX が利用可能ならまずそれを試す（Windows 10+ Python で動く場合あり）
    if hasattr(socket, "AF_UNIX"):
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(path)   # may raise
            sock.setblocking(False)
            reader = asyncio.StreamReader(limit=2**16, loop=loop)
            protocol = asyncio.StreamReaderProtocol(reader)
            transport, _ = await loop.connect_accepted_socket(lambda: protocol, sock)
            writer = asyncio.StreamWriter(transport, protocol, reader, loop)
            return reader, writer
        except Exception:
            # ネイティブで失敗したらフォールバックへ（例: family mismatch）
            try:
                sock.close()
            except Exception:
                pass
            # fall through to winsock ctypes path

    # 2) Winsock via ctypes path (フォールバック)
    fam = getattr(socket, "AF_UNIX", 1)  # module 内で使っている numeric family と一致させる
    # create raw winsock socket
    s_handle = ws2_32.socket(fam, socket.SOCK_STREAM, 0)
    if s_handle == 0 or int(s_handle) == -1:
        err = ws2_32.WSAGetLastError()
        raise OSError(f"WSA socket() failed, err={err}")

    # build sockaddr
    sockaddr = Sockaddr()
    sockaddr.sun_family = fam
    enc = path.encode("utf-8")
    if len(enc) >= UNIX_PATH_MAX:
        # cleanup
        ws2_32.closesocket(s_handle)
        raise ValueError("socket path too long")
    # clear buffer then copy
    tmp = bytearray(sockaddr.sun_data)          # バッファを mutable に取得
    for i in range(UNIX_PATH_MAX):
        tmp[i:i] = b"\x00"
    # sockaddr.sun_data[:len(enc)] = enc
    tmp[:len(enc)] = enc                        # 書き換え
    sockaddr.sun_data = bytes(tmp)              # 再代入（固定長に自動切詰め/拡張）

    # connect
    ret = ws2_32.connect(s_handle, ctypes.byref(sockaddr), ctypes.sizeof(sockaddr))
    if ret != 0:
        err = ws2_32.WSAGetLastError()
        ws2_32.closesocket(s_handle)
        raise OSError(f"ws2_32.connect failed, ret={ret}, WSAGetLastError={err}")

    # wrap raw SOCKET into Python socket object
    try:
        py_sock = socket.socket(fileno=int(s_handle))
        py_sock.setblocking(False)
    except Exception as exc:
        try:
            ws2_32.closesocket(s_handle)
        except Exception:
            pass
        raise

    # hand off to asyncio
    reader = asyncio.StreamReader(limit=2**16, loop=loop)
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.connect_accepted_socket(lambda: protocol, py_sock)
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    return reader, writer

# --- convenience: start_server factory like asyncio.start_server ---
async def start_unix_server(client_connected_cb: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
                            path: str, loop: Optional[asyncio.AbstractEventLoop] = None) -> WindowsUnixServer:
    loop = loop or asyncio.get_event_loop()
    srv = WindowsUnixServer(path, loop=loop)
    await srv.start(client_connected_cb)
    return srv

# End of module
