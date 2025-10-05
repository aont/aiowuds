# AIoWUDS: Async Windows Unix Domain Socket helpers

This repository contains an experimental Python module, `win_unix_asyncio`, that
bridges the gap between Windows and Unix domain sockets for asyncio-based
applications.  Windows only recently gained partial support for `AF_UNIX`; this
module offers a pragmatic shim so that small projects and experiments can
recycle the familiar Unix domain socket API when they have to run on Windows.

## Features

* A `WindowsUnixServer` helper that emulates `asyncio.start_unix_server()` on a
  ProactorEventLoop by combining Winsock and `asyncio` primitives.
* An `open_unix_connection()` coroutine that first tries the native
  `socket.AF_UNIX` implementation (if present) and otherwise falls back to a
  ctypes-based Winsock implementation.
* Minimal ctypes bindings for Winsock functions that are required to create,
  bind, connect, accept, and wait for events on Unix-like sockets.
* Example client and server scripts that demonstrate how to exchange messages
  over a Unix domain socket using asyncio coroutines.

## Project layout

```
win_unix_asyncio.py   # Core module with server and client helpers
example_server.py     # Minimal echo server using WindowsUnixServer
example_client.py     # Simple client that sends a line and prints the reply
```

## Getting started

The examples are written for Python 3.9+ and assume that they are executed on a
Windows machine.  On Unix-like systems the native `asyncio.start_unix_server`
and `asyncio.open_unix_connection` will usually be sufficient, so this helper is
mostly interesting when you want to keep the same code path on Windows.

1. Create a virtual environment and install any dependencies you may require.
   (The examples only rely on the Python standard library.)
2. Start the echo server:

   ```bash
   python example_server.py
   ```

   This will create a socket file named `test.sock` in the working directory and
   keep the server running until you interrupt it.

3. In another terminal run the example client:

   ```bash
   python example_client.py
   ```

   The client connects to `test.sock`, sends a single line, and prints the echo
   response from the server.

## How it works

The `win_unix_asyncio` module performs a best-effort emulation of Unix domain
sockets on Windows by:

1. Ensuring that Winsock is initialised via `WSAStartup`.
2. Using ctypes to access `WSACreateEvent`, `WSAEventSelect`, `WSAWaitForMultipleEvents`,
   `bind`, `accept`, and `connect`.
3. Wrapping accepted or connected raw socket handles with Python's `socket`
   objects and handing them over to the asyncio Proactor event loop via
   `loop.connect_accepted_socket`.
4. Cleaning up the socket file on shutdown to avoid stale endpoints.

Because this is experimental code, you should exercise caution before using it
in production.  Proper error handling, security considerations, and integration
with your application's logging and lifecycle management should be added as
necessary.

## Limitations and caveats

* Requires the asyncio Proactor event loop (default on modern Python releases
  for Windows).  It will raise an error if run on an incompatible loop.
* Socket paths are limited to 108 bytes, matching the traditional Unix domain
  socket length restriction.
* Only basic stream-oriented sockets are supported; datagram mode is currently
  out of scope.
* Thorough testing across different Windows versions has not been carried out.

## Contributing

Issues and pull requests that improve stability, portability, or documentation
are welcome.  Please include details about the Windows version and Python
release when reporting bugs so that compatibility problems can be reproduced and
addressed effectively.

## License

The repository currently does not declare an explicit license.  If you plan to
use the code in a project, please clarify licensing with the maintainer or adapt
it to your needs under an appropriate open-source license.
