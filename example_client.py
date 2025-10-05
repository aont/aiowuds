# example_client.py
import asyncio
from win_unix_asyncio import open_unix_connection

async def client():
    reader, writer = await open_unix_connection("./test.sock")
    writer.write(b"hello world\n")
    await writer.drain()
    line = await reader.readline()
    print("reply:", line)
    writer.close()
    await writer.wait_closed()

if __name__ == "__main__":
    asyncio.run(client())
