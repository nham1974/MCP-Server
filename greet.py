
import os
from fastmcp import FastMCP

server = FastMCP("Sample-SSE-Server")

@server.tool("greet")
async def greet(params):
    name = params.get("name", "Guest")
    return f"Hello, {name}!"
#main
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))

    server.run(
        transport="sse",
        host="0.0.0.0",
        port=port
    )
