# example_echo.py
import asyncio
from pathlib import Path
from win_asyncio_afunix import start_unix_server_win, open_unix_connection_win

SOCKET_PATH = str(Path("./server.sock"))

async def handle_client(stream):
    try:
        while True:
            data = await stream.recv(4096)
            if not data:
                break
            print(data)
            await stream.sendall(b"echo:" + data)
    finally:
        stream.close()

async def run_server():
    server = await start_unix_server_win(SOCKET_PATH, handle_client, unlink_existing=True)
    print(f"listening on {SOCKET_PATH} (Ctrl+C to stop)")
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        server.close()
        await server.wait_closed()

async def run_client(msg: str):
    stream = await open_unix_connection_win(SOCKET_PATH)
    await stream.sendall(msg.encode())
    data = await stream.recv(4096)
    print("client got:", data)
    stream.close()

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", action="store_true", help="Run echo server")
    ap.add_argument("--client", metavar="TEXT", help="Send TEXT to server")
    args = ap.parse_args()

    if args.server:
        asyncio.run(run_server())
    elif args.client:
        asyncio.run(run_client(args.client))
    else:
        ap.print_help()

if __name__ == "__main__":
    main()
