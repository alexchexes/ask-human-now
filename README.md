# Ask Human Now

> MCP server for letting an AI agent ask a human for input through a local dialog,
> Telegram, or both.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io/)

Ask Human Now gives MCP-capable agents a focused tool for cases where guessing is
the wrong move. The agent can pause, show the question and relevant context, wait
for your answer, then continue the same workflow.

## What It Solves

Agents often hit decisions that are not knowable from the repository or local
environment:

- product or design preferences
- risky implementation tradeoffs
- missing credentials, deployment constraints, or domain rules
- ambiguous requirements that should not be guessed
- offline or real-world context that only the human can provide

Ask Human Now exposes one MCP tool, `asking_user_missing_context`, so the agent
can ask directly instead of silently choosing.

## Features

- Native local dialogs on macOS, Linux, and Windows
- Optional Telegram response channel for mobile/away-from-keyboard replies
- `both` mode, where local dialog and Telegram race and the first reply wins
- Local Telegram broker so same-machine concurrent agent sessions do not compete
  for one bot update stream
- Optional timing metadata showing when the prompt was issued and when the local
  dialog times out
- Configurable dialog title, defaulting to `Agent asks...`
- Telegram support for text, files/media up to 20 MB, location, venue, and contact
- Distinctive packaged icons for dialogs and optional Telegram bot branding

## Installation

### uvx

Because the PyPI distribution name is `ask-human-now` and the CLI command is
`ask-human`, run it with `--from`:

```json
{
  "mcpServers": {
    "ask-human": {
      "command": "uvx",
      "args": ["--from", "ask-human-now", "ask-human", "--transport", "stdio"]
    }
  }
}
```

### pip

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install ask-human-now
ask-human --help
```

Then point your MCP client at the installed command:

```json
{
  "mcpServers": {
    "ask-human": {
      "command": "ask-human",
      "args": ["--transport", "stdio"]
    }
  }
}
```

## MCP Client Setup

Always check your client documentation for the latest config format:

- Codex MCP docs: <https://developers.openai.com/codex/mcp>
- Claude Code MCP docs: <https://docs.anthropic.com/en/docs/claude-code/mcp>
- Cursor MCP docs: <https://docs.cursor.com/context/model-context-protocol>

### Codex

Add a server to `~/.codex/config.toml`:

```toml
[mcp_servers.ask-human]
command = "ask-human"
args = ["--transport", "stdio", "--dialog-title", "Codex Needs Input"]
tool_timeout_sec = 1200
```

For Telegram and longer waits:

```toml
[mcp_servers.ask-human]
command = "ask-human"
args = [
  "--transport", "stdio",
  "--dialog-title", "Codex Needs Input",
  "--timeout-seconds", "86400",
  "--show-timing-info",
  "--response-channel", "both",
  "--telegram", "<bot_token> <chat_id>",
]
tool_timeout_sec = 86400
```

Restart the Codex session after changing MCP config. To make Codex prefer the
tool for risky ambiguity, add an instruction such as:

```md
If a required fact or preference cannot be discovered locally and a wrong
assumption could affect correctness, safety, architecture, or user intent, use
the `ask-human` tool before proceeding.
```

### Claude Code

One typical setup is to add the server through the Claude Code MCP command:

```bash
claude mcp add --transport stdio ask-human -- uvx --from ask-human-now ask-human --transport stdio
```

If your Claude Code version uses a different MCP configuration format, use the
official docs linked above and keep the command/arguments the same:

```text
command: uvx
args: --from ask-human-now ask-human --transport stdio
```

### Cursor

Add this to your Cursor MCP config:

```json
{
  "mcpServers": {
    "ask-human": {
      "command": "uvx",
      "args": ["--from", "ask-human-now", "ask-human", "--transport", "stdio"]
    }
  }
}
```

### Local Development

```json
{
  "mcpServers": {
    "ask-human-dev": {
      "command": "python",
      "args": ["-m", "ask_human_now", "--transport", "stdio"],
      "cwd": "/path/to/ask-human-now",
      "env": {
        "PYTHONPATH": "/path/to/ask-human-now/src"
      }
    }
  }
}
```

The included `mcp-server-config.json` has copyable examples for installed,
`uvx`, and local-dev usage.

## Command Reference

### Transports

STDIO is the default and is what most local MCP clients use:

```bash
ask-human --transport stdio
```

SSE is available for clients that connect over HTTP:

```bash
ask-human --transport sse --host 0.0.0.0 --port 8080
```

### Dialog Title

The default dialog title is `Agent asks...`.

```bash
ask-human --transport stdio --dialog-title "Codex Needs Input"
```

### Timeout

The local dialog timeout defaults to 120 seconds.

```bash
ask-human --transport stdio --timeout-seconds 1200
```

MCP clients may enforce their own tool-call timeout. If your client supports a
tool timeout setting, set it to at least the same value as `--timeout-seconds`;
otherwise the client may stop waiting before Ask Human Now does.

### Timing Metadata

Use `--show-timing-info` to include compact timing metadata in dialogs and
Telegram prompts:

```bash
ask-human --transport stdio --show-timing-info
```

The timing text uses the current OS short date/time format where available.

### Response Channels

Use `--response-channel` to choose where replies are collected:

- `dialog`: local native dialog only, default
- `telegram`: Telegram only
- `both`: local dialog and Telegram at the same time; first reply wins

```bash
ask-human --transport stdio --response-channel telegram --telegram "<bot_token> <chat_id>"
```

Optional Telegram file download directory:

```bash
ask-human \
  --transport stdio \
  --response-channel telegram \
  --telegram "<bot_token> <chat_id>" \
  --telegram-download-dir "~/Downloads/ask-human"
```

`--telegram-download-dir` defaults to a folder under the system temp directory.
It supports `~`, environment variables such as `%USERPROFILE%`, and `{cwd}`.

<details>
<summary>Create a Telegram bot and find the chat id</summary>

1. Open Telegram and message `@BotFather`.
2. Run `/newbot` and follow BotFather's prompts.
3. Copy the bot token.
4. Send any message to your new bot.
5. Open this URL in a browser, replacing `<bot_token>`:

   ```text
   https://api.telegram.org/bot<bot_token>/getUpdates
   ```

6. Find `message.chat.id` in the JSON response. That is the `<chat_id>`.
7. Keep the bot token secret. Anyone with the token can control the bot.

For a group chat, add the bot to the group, send a message in the group, then
call `getUpdates` and use the group's `chat.id`.

</details>

## Telegram Broker

Telegram delivery uses a local auto-started broker process instead of letting
each agent session poll `getUpdates` independently.

Current behavior:

- one local broker is created per Telegram target (`bot_token + chat_id`)
- sessions on the same machine that use the same target reuse that broker
- different Telegram targets on the same machine use different brokers
- broker discovery uses persisted local state plus a health check
- the broker binds to `127.0.0.1` on an OS-assigned free port by default

This makes same-machine concurrent Telegram prompts safe.

Current limitation:

- cross-machine or shared-server coordination is not implemented yet
- if two different machines use the same bot target at the same time, replies
  can still be consumed by the wrong machine

Telegram prompt metadata includes:

- `Prompt ID: ...`
- `Broker: <label> [<id>]`

That helps identify which local broker instance sent a prompt and makes some
cross-machine mix-ups easier to diagnose.

Advanced/manual broker mode is mainly for debugging and future remote deployment:

```bash
ask-human --telegram-broker --telegram "<bot_token> <chat_id>"
```

Stop a local broker on Windows for testing or troubleshooting:

```powershell
Get-CimInstance Win32_Process |
  Where-Object {
    $_.Name -like 'python*' -and
    $_.CommandLine -like '*ask_human_now*' -and
    $_.CommandLine -like '*--telegram-broker*'
  } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

The next Telegram prompt auto-starts a fresh local broker if one is needed.

In `both` mode:

- macOS and Linux try to close the local dialog when the Telegram reply arrives first
- Windows keeps the current Tk dialog behavior; if Telegram wins first, the local
  dialog may stay open and any later answer there will be ignored

Telegram reply behavior:

- use Telegram's Reply feature on the bot's question message
- if a local broker is actively waiting and you send a non-reply message, it sends
  a short warning that the message is ignored and you must use Reply
- if you reply to a message that is not the currently active question, it sends
  a warning instead of silently consuming the reply
- if you reply to one of this broker's own older inactive prompt messages, it sends
  a short warning that the old question is no longer active
- successful replies get a `Received [Prompt ID]` acknowledgement
- supported replies include text, single files/media messages up to 20 MB,
  location, venue, and contact
- albums/media groups are not supported yet; reply again with a single message
- files are downloaded locally and returned to the agent as local paths
- replies that appear intended for another broker instance trigger a warning
  instead of being silently misrouted
- Telegram delivery failures for the initial question or retry/warning messages
  are returned to the agent as prompt errors

## Telegram Bot Icons

Packaged icons are available under `src/ask_human_now/assets/`:

- `icon-color-round.png`
- `icon-color-round-alt.png`

You can use either as a Telegram bot profile photo so Ask Human Now prompts are
easy to distinguish on mobile.

## Tool Reference

### `asking_user_missing_context`

Ask the user for missing context during agent workflows.

Parameters:

- `question` (string, required): specific question, max 1000 characters
- `context` (string, optional): short background, max 2000 characters

Returns:

- `User response: ...` when the user answers
- `Empty response received` when the user clicks OK without text
- `Timeout: ...` when no response arrives in time
- `Cancelled: ...` when the user cancels
- `Error: ...` for validation or system failures

Example tool call:

```python
asking_user_missing_context(
    question="Should this import overwrite an existing session or stop?",
    context="Both behaviors are possible, but choosing wrong could lose user data."
)
```

## Development

Requirements:

- Python 3.10+
- macOS: `osascript`
- Linux: `zenity`
- Windows: `tkinter`

Install for development:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Run checks:

```bash
black --check .
isort --check-only .
mypy src
pyright
pytest
```

Build locally:

```bash
python -m build
python -m twine check dist/*
```

Project structure:

```text
ask-human-now/
├── src/ask_human_now/
│   ├── assets/
│   ├── broker_state.py
│   ├── dialogs.py
│   ├── prompt_formatting.py
│   ├── server.py
│   ├── telegram_broker.py
│   ├── telegram_broker_client.py
│   ├── telegram_client.py
│   └── telegram_models.py
├── tests/
├── pyproject.toml
└── README.md
```

## Security And Privacy

- Local dialog prompts stay on your machine.
- Telegram prompts and replies go through Telegram when that channel is enabled.
- Telegram files are downloaded to a local directory and returned as paths.
- Bot tokens should be treated as secrets.
- Ask Human Now does not run a remote server by default.

## Fork And Attribution

Ask Human Now is a maintained fork of the original
`ask-human-for-context-mcp` project. The upstream MIT license notice is retained
in [LICENSE](LICENSE), and this fork adds new package identity, Telegram support,
local broker coordination, additional configuration, and release infrastructure.

## License

MIT License. See [LICENSE](LICENSE).

## Support

- Issues: <https://github.com/alexchexes/ask-human-now/issues>
- Model Context Protocol: <https://modelcontextprotocol.io/>
