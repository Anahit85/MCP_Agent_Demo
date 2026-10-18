"""MCP server with three example tools.

What this file is
-----------------
A small MCP (Model Context Protocol) server. MCP is an open standard that lets an
AI application discover and call external "tools" in a uniform way. A *server*
exposes tools, and a *client* uses them. Here, the client is ``mcp_agent_demo.py``.

What it contains
----------------
Three tools that Claude can call through the agent:

* ``add(a, b)``          - adds two numbers
* ``word_count(text)``   - counts the words in a text
* ``get_current_time()`` - returns the current local date and time with UTC offset

How tools are described to Claude
---------------------------------
The ``@mcp.tool()`` decorator registers a function as a tool:

* the function name becomes the tool name,
* the docstring becomes the tool description that Claude reads to decide
  when the tool is useful,
* the type hints become the JSON input schema.

How it runs
-----------
The server communicates over "stdio" (standard input and output). You normally do
not start it yourself, because ``mcp_agent_demo.py`` launches it as a subprocess.

Requirements
------------
    pip install "mcp>=2"

(In MCP SDK 2.x the server class is called ``MCPServer``. Older versions called it
``FastMCP``.)
"""

from datetime import datetime

from mcp.server.mcpserver import MCPServer

# The server object. The name is shown to MCP clients when they connect.
mcp = MCPServer("demo-server")


@mcp.tool()
def add(a: float, b: float) -> float:
    """Add two numbers.

    Args:
        a: The first number.
        b: The second number.

    Returns:
        The sum a + b.
    """
    return a + b


@mcp.tool()
def word_count(text: str) -> int:
    """Count the words in a text.

    Words are separated by whitespace (spaces, tabs or line breaks).

    Args:
        text: The text to analyze.

    Returns:
        The number of words in the text.
    """
    return len(text.split())


@mcp.tool()
def get_current_time() -> str:
    """Get the current local date and time.

    Returns:
        A string such as "2026-10-18 14:05:09+0200" (date, time and UTC offset).
    """
    # astimezone() attaches the local time zone, so the result is not "naive".
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")


if __name__ == "__main__":
    # Start the server on stdio. It then waits for MCP messages from a client.
    mcp.run()