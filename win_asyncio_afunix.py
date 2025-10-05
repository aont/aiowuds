"""
win_asyncio_afunix.py
---------------------
Minimal asyncio-friendly AF_UNIX server/client for Windows 10+ (build 17063+).

Uses direct Winsock bind/connect via ctypes to avoid Python's socket.bind bad-family errors,
and integrates with asyncio's ProactorEventLoop via proactor.wait_for_handle().
"""

from __future__ import annotations
import asyncio
import os
import socket
import ctypes
from ctypes import wintypes

# ---------- constants / small helpers ----------
SUN_PATH_MAX = 108  # traditional sockaddr_un sun_path size

PTR_SIZE = ctypes.sizeof(ctypes.c_void_p)
if PTR_SIZE == 8:
    SOCKET_T = ctypes.c_uint64
else:
    SOCKET_T = ctypes.c_uint32

WSAEVENT = wintypes.HANDLE

# Winsock event constants (subset)
FD_READ = 0x01
FD_WRITE = 0x02
FD_OOB = 0x04
FD_ACCEPT = 0x08
FD_CONNECT = 0x10
FD_CLOSE = 0x20

FD_CONNECT_BIT = 4
FD_CLOSE_BIT = 5

INVALID_SOCKET = SOCKET_T(-1).value

# ---------- ctypes structures ----------
class WSANETWORKEVENTS(ctypes.Structure):
    _fields_ = [
        ("lNetworkEvents", ctypes.c_long),
        ("iErrorCode", ctypes.c_int * 10),
    ]

class Sockaddr(ctypes.Structure):
    # mimic sockaddr_un minimal: family + path buffer
    _fields_ = [
        ("sun_family", ctypes.c_ushort),
        ("sun_path", ctypes.c_char * SUN_PATH_MAX),
    ]

# ---------- load ws2_32 and set prototypes ----------
ws2_32 = ctypes.windll.ws2_32

ws2_32.WSACreateEvent.restype = WSAEVENT
ws2_32.WSACreateEvent.argtypes = ()

ws2_32.WSACloseEvent.restype = wintypes.BOOL
ws2_32.WSACloseEvent.argtypes = (WSAEVENT,)

ws2_32.WSAResetEvent.restype = wintypes.BOOL
ws2_32.WSAResetEvent.argtypes = (WSAEVENT,)

ws2_32.WSAEventSelect.restype = ctypes.c_int
ws2_32.WSAEventSelect.argtypes = (SOCKET_T, WSAEVENT, ctypes.c_long)

ws2_32.WSAEnumNetworkEvents.restype = ctypes.c_int
ws2_32.WSAEnumNetworkEvents.argtypes = (SOCKET_T, WSAEVENT, ctypes.POINTER(WSANETWORKEVENTS))

ws2_32.WSAGetLastError.restype = ctypes.c_int
ws2_32.WSAGetLastError.argtypes = ()

# accept/bind/connect prototypes
ws2_32.accept.restype = SOCKET_T
ws2_32.accept.argtypes = (SOCKET_T, ctypes.c_void_p, ctypes.c_void_p)

ws2_32.bind.restype = ctypes.c_int
ws2_32.bind.argtypes = (SOCKET_T, ctypes.c_void_p, ctypes.c_int)

ws2_32.connect.restype = ctypes.c_int
ws2_32.connect.argtypes = (SOCKET_T, ctypes.c_void_p, ctypes.c_int)

# ---------- helpers ----------
def _check_zero(ret: int, what: str):
    if ret != 0:
        err = ws2_32.WSAGetLastError()
        raise OSError(err, f"{what} failed: WSAGetLastError={err}")

def _check_socket(res: int, what: str):
    if res == INVALID_SOCKET:
        err = ws2_32.WSAGetLastError()
        raise OSError(err, f"{what} failed: WSAGetLastError={err}")

# ---------- AFUnixStream ----------
class AFUnixStream:
    def __init__(self, sock: socket.socket, loop: asyncio.AbstractEventLoop | None = None):
        self.sock = sock
        self.loop = loop or asyncio.get_running_loop()
        self._closed = False

    async def recv(self, n: int = 4096) -> bytes:
        if self._closed:
            return b""
        return await self.loop.sock_recv(self.sock, n)

    async def sendall(self, data: bytes) -> None:
        if self._closed:
            raise RuntimeError("sendall on closed AFUnixStream")
        await self.loop.sock_sendall(self.sock, data)

    def close(self) -> None:
        if not self._closed:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
            self._closed = True

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.close()
        await self.wait_closed()

# ---------- Server ----------
class AFUnixServerWin:
    def __init__(self, path: str, *, backlog: int = 100, unlink_existing: bool = True,
                 loop: asyncio.AbstractEventLoop | None = None):
        self.path = path
        self.backlog = backlog
        self.unlink_existing = unlink_existing
        self.loop = loop or asyncio.get_running_loop()
        self._task: asyncio.Task | None = None
        self._closed = False

        # ensure AF_UNIX exists in socket module on Windows
        if not hasattr(socket, "AF_UNIX"):
            socket.AF_UNIX = 1

        if unlink_existing and os.path.exists(path):
            os.unlink(path)

        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.setblocking(False)

        # ---- do direct Winsock bind using sockaddr structure ----
        addr = Sockaddr()
        addr.sun_family = socket.AF_UNIX
        enc = path.encode("utf-8")
        if len(enc) >= SUN_PATH_MAX:
            raise ValueError(f"AF_UNIX path too long (max {SUN_PATH_MAX - 1} bytes)")
        # write into the c_char array by slice assignment (leaves remaining bytes as zeros)
        tmp = bytearray(addr.sun_path)
        tmp[:len(enc)] = enc
        addr.sun_path = bytes(tmp)

        ret = ws2_32.bind(self.sock.fileno(), ctypes.byref(addr), ctypes.sizeof(addr))
        if ret != 0:
            err = ws2_32.WSAGetLastError()
            raise OSError(err, f"ws2_32.bind failed: WSA error {err}")

        self.sock.listen(self.backlog)

        # create and register accept event
        self._accept_event = ws2_32.WSACreateEvent()
        if not self._accept_event:
            raise OSError("WSACreateEvent failed")

        _check_zero(ws2_32.WSAEventSelect(self.sock.fileno(), self._accept_event, FD_ACCEPT | FD_CLOSE),
                    "WSAEventSelect(FD_ACCEPT|FD_CLOSE)")

    def start(self, client_connected_cb):
        if self._task is not None:
            raise RuntimeError("Server already started")
        self._task = asyncio.create_task(self._accept_loop(client_connected_cb))
        return self

    async def _accept_loop(self, client_connected_cb):
        proactor = getattr(self.loop, "_proactor", None)
        if proactor is None:
            raise RuntimeError("This event loop does not expose a proactor. Use ProactorEventLoop on Windows.")

        try:
            while not self._closed:
                await proactor.wait_for_handle(self._accept_event)
                events = WSANETWORKEVENTS()
                _check_zero(ws2_32.WSAEnumNetworkEvents(self.sock.fileno(), self._accept_event,
                                                        ctypes.byref(events)),
                            "WSAEnumNetworkEvents")

                if events.lNetworkEvents & FD_CLOSE:
                    break

                if events.lNetworkEvents & FD_ACCEPT:
                    # Accept all queued connections
                    while True:
                        client_fd = ws2_32.accept(self.sock.fileno(), None, None)
                        if client_fd == INVALID_SOCKET:
                            break
                        client = socket.socket(fileno=int(client_fd))
                        client.setblocking(False)
                        stream = AFUnixStream(client, self.loop)
                        try:
                            res = client_connected_cb(stream)
                            if asyncio.iscoroutine(res):
                                asyncio.create_task(res)
                        except Exception:
                            stream.close()
                            raise
        finally:
            # ensure event reset/cleanup
            try:
                ws2_32.WSAResetEvent(self._accept_event)
            except Exception:
                pass

    def close(self) -> None:
        self._closed = True
        try:
            self.sock.close()
        finally:
            if getattr(self, "_accept_event", None):
                try:
                    ws2_32.WSACloseEvent(self._accept_event)
                except Exception:
                    pass
                self._accept_event = None
        if self.unlink_existing:
            try:
                if os.path.exists(self.path):
                    os.unlink(self.path)
            except OSError:
                pass

    async def wait_closed(self) -> None:
        if self._task:
            try:
                await asyncio.shield(self._task)
            except Exception:
                pass

# ---------- Client helper ----------
async def open_unix_connection_win(path: str) -> AFUnixStream:
    if not hasattr(socket, "AF_UNIX"):
        socket.AF_UNIX = 1

    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.setblocking(False)

    connect_event = ws2_32.WSACreateEvent()
    if not connect_event:
        sock.close()
        raise OSError("WSACreateEvent failed for connect")

    try:
        _check_zero(ws2_32.WSAEventSelect(sock.fileno(), connect_event, FD_CONNECT | FD_CLOSE),
                    "WSAEventSelect(FD_CONNECT|FD_CLOSE)")

        # prepare sockaddr and attempt non-blocking connect via ws2_32.connect
        addr = Sockaddr()
        addr.sun_family = socket.AF_UNIX
        enc = path.encode("utf-8")
        if len(enc) >= SUN_PATH_MAX:
            sock.close()
            raise ValueError(f"AF_UNIX path too long (max {SUN_PATH_MAX - 1} bytes)")
        tmp = bytearray(addr.sun_path)
        tmp[:len(enc)] = enc
        addr.sun_path = bytes(tmp)

        ret = ws2_32.connect(sock.fileno(), ctypes.byref(addr), ctypes.sizeof(addr))
        if ret != 0:
            err = ws2_32.WSAGetLastError()
            # 10035 == WSAEWOULDBLOCK (non-blocking connect in progress)
            if err not in (10035,):
                sock.close()
                raise OSError(err, f"ws2_32.connect failed: {err}")

        proactor = getattr(loop, "_proactor", None)
        if proactor is None:
            sock.close()
            raise RuntimeError("This event loop does not expose a proactor.")

        # wait for FD_CONNECT/FD_CLOSE
        await proactor.wait_for_handle(connect_event)
        events = WSANETWORKEVENTS()
        _check_zero(ws2_32.WSAEnumNetworkEvents(sock.fileno(), connect_event, ctypes.byref(events)),
                    "WSAEnumNetworkEvents(connect)")

        if events.lNetworkEvents & FD_CONNECT:
            err = events.iErrorCode[FD_CONNECT_BIT]
            if err != 0:
                sock.close()
                raise OSError(err, f"connect({path!r}) failed: {err}")

        return AFUnixStream(sock, loop)
    finally:
        if connect_event:
            try:
                ws2_32.WSACloseEvent(connect_event)
            except Exception:
                pass

# ---------- convenience helper ----------
async def start_unix_server_win(path: str, client_connected_cb, *, backlog: int = 100, unlink_existing: bool = True):
    srv = AFUnixServerWin(path, backlog=backlog, unlink_existing=unlink_existing)
    srv.start(client_connected_cb)
    return srv
