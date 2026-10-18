"""MCP agent demo: Claude uses tools provided by an MCP server.

What this file is
-----------------
A small AI agent. It connects to a local MCP (Model Context Protocol) server,
discovers the tools that server offers and lets Claude decide when to call them.
Together with ``mcp_server.py`` this shows the full loop of a tool-using agent.

How it works
------------
1. The agent starts ``mcp_server.py`` as a subprocess and connects to it over stdio.
2. It asks the server which tools exist and converts them to the format the
   Claude API expects.
3. For every question you type, the agent sends the question and the tool
   definitions to Claude.
4. If Claude replies with a ``tool_use`` request, the agent runs that tool on the
   MCP server and sends the result back to Claude.
5. Steps 3 and 4 repeat until Claude answers with plain text. That text is the
   final answer, and it is printed.

What this file contains
-----------------------
Constants:
    MODEL       - the Claude model that is used
    MAX_TOKENS  - maximum length of each Claude response
    MAX_STEPS   - safety limit for tool-call rounds per question
    SERVER      - how to start the MCP server (command and arguments)

Functions:
    create_client()        - builds the async Claude client from environment settings
    to_anthropic_tools()   - converts MCP tool descriptions to the Claude tool format
    run_tool()             - runs one tool on the MCP server
    ask()                  - the agent loop for a single question
    main()                 - connects to the server and runs the interactive prompt

Setup
-----
    pip install anthropic "mcp>=2" python-dotenv

Create a ``.env`` file in the same folder (never commit it to Git):

    ANTHROPIC_API_KEY=your_full_key
    # Only needed if your key is not tied to a workspace:
    # ANTHROPIC_WORKSPACE_ID=wrkspc_...

Run
---
    python mcp_agent_demo.py

Type a question and press Enter. Type "exit" to quit.
"""

import asyncio
import os
import sys
from pathlib import Path

from anthropic import (
    APIConnectionError,
    AsyncAnthropic,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
)
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Read the .env file. override=True means that the values in .env take priority
# over any old environment variables that are already set in Windows.
load_dotenv(override=True)

# The Claude model that answers the questions.
MODEL = "claude-sonnet-5-5"

# Maximum number of tokens in each Claude response.
MAX_TOKENS = 1000

# Safety limit: how many times Claude may request tools for one question.
# This prevents an endless loop if the model keeps calling tools.
MAX_STEPS = 10

# How to start the MCP server: run mcp_server.py (in the same folder as this file)
# with the same Python interpreter. The client talks to it over stdio.
SERVER = StdioServerParameters(
    command=sys.executable,
    args=[str(Path(__file__).with_name("mcp_server.py"))],
)


def create_client() -> AsyncAnthropic:
    """Create the async Claude API client from environment settings.

    Reads ``ANTHROPIC_API_KEY`` (required) and ``ANTHROPIC_WORKSPACE_ID``
    (optional). The workspace ID is only needed if the key is not tied to a
    single workspace. The SDK adds the authentication headers itself, so no
    manual headers are needed.

    Returns:
        A ready-to-use ``AsyncAnthropic`` client.

    Raises:
        SystemExit: If ``ANTHROPIC_API_KEY`` is not set.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("Error: ANTHROPIC_API_KEY is missing (set it in .env or as an environment variable).")

    workspace_id = os.getenv("ANTHROPIC_WORKSPACE_ID")
    headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None

    return AsyncAnthropic(api_key=api_key, default_headers=headers)


def to_anthropic_tools(mcp_tools) -> list[dict]:
    """Convert MCP tool descriptions to the format the Claude API expects.

    MCP and the Claude API describe tools almost identically. Each tool needs a
    name, a description (which Claude reads to decide when to use the tool) and a
    JSON schema that describes the input arguments.

    Args:
        mcp_tools: The list of tools returned by ``session.list_tools()``.

    Returns:
        A list of dictionaries with the keys ``name``, ``description`` and
        ``input_schema``, ready to be passed as ``tools=`` to the Claude API.
    """
    return [
        {
            "name": tool.name,
            "description": tool.description or "",
            "input_schema": tool.input_schema,
        }
        for tool in mcp_tools
    ]


async def run_tool(session: ClientSession, name: str, arguments: dict) -> tuple[str, bool]:
    """Run one tool on the MCP server.

    Errors are not raised. They are returned as text with an error flag, so that
    they can be reported back to Claude, which can then react to the problem
    instead of the whole program stopping.

    Args:
        session: The open MCP client session.
        name: The name of the tool to run, for example ``"add"``.
        arguments: The input arguments for the tool, for example ``{"a": 1, "b": 2}``.

    Returns:
        A tuple ``(text, is_error)``. ``text`` is the tool's text output (or an
        error message) and ``is_error`` is True if the tool call failed.
    """
    try:
        result = await session.call_tool(name, arguments)
    except Exception as e:  # noqa: BLE001 - errors should go to Claude, not stop the program
        return f"Error while running {name}: {e}", True

    # A tool result can contain several content blocks. Keep only the text blocks.
    text = "\n".join(c.text for c in result.content if getattr(c, "type", None) == "text")
    return text, bool(result.is_error)


async def ask(client: AsyncAnthropic, session: ClientSession, tools: list[dict], question: str) -> str:
    """Ask Claude one question. Claude may call tools several times on the way.

    This is the agent loop. It sends the conversation to Claude. If Claude asks to
    use tools, the agent runs them on the MCP server, appends the results to the
    conversation and asks Claude again. The loop ends when Claude answers with
    plain text, or after ``MAX_STEPS`` rounds.

    Note:
        Every question starts a new conversation. The agent does not remember
        earlier questions.

    Args:
        client: The async Claude client.
        session: The open MCP client session, used to run the requested tools.
        tools: The tool definitions in Claude format (see ``to_anthropic_tools``).
        question: The user's question.

    Returns:
        Claude's final text answer, or an abort message if too many tool rounds
        were needed.
    """
    messages: list[dict] = [{"role": "user", "content": question}]

    for _ in range(MAX_STEPS):
        response = await client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            tools=tools,
            messages=messages,
        )
        # Add Claude's reply to the conversation (it may contain tool requests).
        messages.append({"role": "assistant", "content": response.content})

        # No tool request means this is the final answer.
        if response.stop_reason != "tool_use":
            return "".join(b.text for b in response.content if b.type == "text")

        # Run every requested tool and collect the results.
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"  -> Tool: {block.name}({block.input})")
                text, is_error = await run_tool(session, block.name, block.input)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,  # links the result to Claude's request
                        "content": text,
                        "is_error": is_error,
                    }
                )
        # Send the results back to Claude as a user message.
        messages.append({"role": "user", "content": tool_results})

    return "Aborted: Claude needed too many tool steps."


async def main() -> None:
    """Connect to the MCP server and run the interactive question loop.

    Starts the MCP server, opens a client session, lists the available tools and
    then repeatedly reads a question from the keyboard and prints Claude's answer.
    API errors (401, 400, 404, connection problems) are caught and explained in
    plain language. Type ``exit`` (or ``quit`` / ``q``) to end the program.
    """
    client = create_client()

    # Start the server subprocess and open the client session in one statement.
    async with stdio_client(SERVER) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        # Discover the tools that the MCP server offers.
        tools_response = await session.list_tools()
        tools = to_anthropic_tools(tools_response.tools)
        print(f"Connected to MCP server. Tools: {', '.join(t['name'] for t in tools)}")
        print(f"Model: {MODEL}. Type 'exit' to quit.")

        while True:
            # input() blocks, so it runs in a thread to keep the async loop responsive.
            question = (await asyncio.to_thread(input, "\nYou: ")).strip()
            if question.lower() in {"exit", "quit", "q"}:
                print("Goodbye!")
                break
            if not question:
                continue

            try:
                answer = await ask(client, session, tools, question)
            except AuthenticationError:
                print(
                    "\n401: API key invalid, incomplete or expired.\n"
                    "Create a new key in the Console, copy it right after creating it "
                    "and paste the full key into .env."
                )
                break
            except BadRequestError as e:
                print(f"\n400: Invalid request: {e}")
                print("If this is about 'anthropic-workspace-id', set ANTHROPIC_WORKSPACE_ID in .env.")
                break
            except NotFoundError as e:
                print(f"\n404: {e}\nCheck the workspace ID or the model name.")
                break
            except APIConnectionError:
                print("\nNo connection to the API. Check your internet connection.")
                continue

            print(f"\nClaude: {answer}")


if __name__ == "__main__":
    asyncio.run(main())