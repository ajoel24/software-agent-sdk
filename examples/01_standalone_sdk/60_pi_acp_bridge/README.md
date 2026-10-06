# Pi ACP Bridge

Use [pi](https://pi.dev) as an ACP-compatible agent backend for OpenHands.

## Overview

OpenHands `ACPAgent` delegates to an ACP (Agent Client Protocol) server.
This bridge exposes pi's agent capabilities as an ACP server, allowing
OpenHands conversations to run on pi instead of direct LLM calls.

```
OpenHands Conversation → ACPAgent → pi-acp-bridge → pi (RPC mode)
```

## Requirements

- `pi` installed and on PATH (`npm install -g @earendil-works/pi-coding-agent`)
- `LLM_API_KEY` set (or pi auth configured)

## Usage

```bash
uv run examples/01_standalone_sdk/60_pi_acp_bridge/main.py
```

## Environment

| Variable      | Default                 | Description              |
|---------------|-------------------------|--------------------------|
| `PI_CWD`      | current dir             | Working directory for pi |
| `PI_MODEL`    | auto                    | Model override           |
| `LLM_API_KEY` | (required)              | API key for the model    |
