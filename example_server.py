# example_server.py
import asyncio
from win_unix_asyncio import start_unix_server

async def handle_client(reader, writer):
    addr = writer.get_extra_info("peername")
    print("client connected:", addr)
    try:
        while True:
            data = await reader.readline()
            if not data:
                break
            print("recv:", data)
            writer.write(b"OK: " + data)
            await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()
        print("client disconnected")

async def main():
    srv = await start_unix_server(handle_client, "./test.sock")
    print("server running on ./test.sock")
    # keep running until Ctrl-C
    try:
        await asyncio.Event().wait()
    finally:
        await srv.close()

if __name__ == "__main__":
    asyncio.run(main())
