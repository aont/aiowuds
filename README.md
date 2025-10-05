# win-asyncio-afunix

Asyncio helpers that enable using UNIX domain sockets on Windows 10 build 17063
or newer. The module exposes a small API that mirrors the high level asyncio
constructs available on Unix platforms so existing socket-based code can be
ported with minimal changes.

## Installation

The project is published as a simple Python module and can be installed with
``pip``:

```bash
pip install win-asyncio-afunix
```

To work on the project locally you can install it from the repository root:

```bash
pip install .
```

## Usage

```python
import asyncio
from win_asyncio_afunix import start_unix_server_win, open_unix_connection_win

SOCKET_PATH = "./demo.sock"

async def handle_client(stream):
    while True:
        chunk = await stream.recv(4096)
        if not chunk:
            break
        await stream.sendall(chunk)

async def main():
    server = await start_unix_server_win(SOCKET_PATH, handle_client)
    async with await open_unix_connection_win(SOCKET_PATH) as client:
        await client.sendall(b"hello")
        reply = await client.recv(4096)
        print(reply)
    server.close()
    await server.wait_closed()

asyncio.run(main())
```

A more complete example that offers an echo server/client pair is available in
[`example_echo.py`](./example_echo.py).

## Compatibility

* Python 3.8+
* Windows 10 build 17063 or newer (AF_UNIX support)
* ``asyncio`` Proactor event loop

## Development

The project is intentionally lightweight and does not ship with a large amount
of tooling. You can run the example echo application while developing:

```bash
# Terminal 1
python example_echo.py --server

# Terminal 2
python example_echo.py --client "Hello from client"
```

Pull requests and issues are welcome.
