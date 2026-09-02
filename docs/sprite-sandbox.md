---
title: Sprite Sandbox
description: Give a Pydantic AI agent a persistent Sprite with command and file tools.
---

# Sprite Sandbox

`SpriteSandbox` gives an agent a persistent Linux computer for running commands
and working with files. Use it for coding, data processing, and long-running
tasks that should not execute model-generated commands on the application host.

The capability is backed by [Sprites](https://docs.sprites.dev/). By default,
each agent run gets a fresh Sprite that is destroyed when the run ends. You can
also attach to an existing Sprite or reuse one across several runs.

> While Pydantic AI Harness is on 0.x releases, the API may change between minor releases; when it does, deprecation warnings and release-note migration guidance tell you (or your agent) exactly how to upgrade. See the [version policy](index.md#version-policy).

## Quick start

Install the `sprites` extra and set a Sprites API token:

```bash
uv add "pydantic-ai-harness[sprites]"
export SPRITE_TOKEN=...
```

```python
from pydantic_ai import Agent
from pydantic_ai_harness import SpriteSandbox

agent = Agent(
    'anthropic:claude-fable-5',
    capabilities=[SpriteSandbox()],
)

result = agent.run_sync('Create a Python program and run it.')
print(result.output)
```

The capability adds four tools:

| Tool | Purpose |
| --- | --- |
| `run_command` | Run a Bash command with a bounded timeout and combined output. |
| `read_file` | Read a UTF-8 file with bounded output and line paging. |
| `write_file` | Write a UTF-8 file and create parent directories. |
| `list_directory` | List entries, marking directories with `/`. |

## Lifecycle

The default mode creates a uniquely named Sprite with an ownership label per
agent run. On exit, the session fetches the Sprite and verifies that label before
destroying it. If the label changed, cleanup raises
`SpriteSandboxOwnershipError` instead of risking deletion of another Sprite.

Attach to a Sprite you manage by name. It is left running when the run ends:

```python
from pydantic_ai_harness import SpriteSandbox

SpriteSandbox(sprite_name='my-existing-sprite')
```

Reuse one Sprite across multiple runs with a caller-owned session:

```python
from pydantic_ai import Agent
from pydantic_ai_harness import SpriteSandbox
from pydantic_ai_harness.sprites import SpriteSandboxSession

async with SpriteSandboxSession() as session:
    agent = Agent(
        'anthropic:claude-fable-5',
        capabilities=[SpriteSandbox(session=session)],
    )
    await agent.run('Install the project dependencies.')
    await agent.run('Run the tests in the same Sprite.')
```

An injected session must already be open. The capability never opens, closes,
or destroys it. Attached and injected Sprites can retain files and processes, so
do not share one between overlapping runs that need isolation.

## Timeouts and output limits

Every command gets a finite deadline. `default_command_timeout` supplies the
normal limit and `max_command_timeout` caps model-supplied values. The command
runs in a process group inside the Sprite, so a timeout terminates the shell and
its child processes.

The in-Sprite byte cut preserves the beginning and end of combined stdout and
stderr before the SDK returns them. The tool layer then applies
`max_output_bytes` and `max_output_lines` to the retained payload, keeping the
tail where diagnostics commonly appear. All cuts are marked. Truncation markers
and timeout or exit annotations are added after the payload limits, so the final
tool result can be slightly larger than either configured cap.

`read_file` checks file size before reading and checks the returned byte count
again. A file that grows between those operations can temporarily exceed
`max_read_bytes` in client memory before being rejected. `list_directory`
materializes the complete listing before truncation. Use bounded shell commands
for virtual files or unusually large directories.

The Sprites Python SDK is synchronous. The capability runs its calls in worker
threads, so SDK requests do not block the agent event loop.

## Errors and composition

Recoverable command and filesystem failures become model retry prompts. A
missing Sprite raises `SpriteSandboxUnavailableError` and rejected credentials
raise `SpriteSandboxAuthError`; both are terminal because repeating a tool call
cannot fix them.

Do not combine this capability with another unprefixed capability that registers
`run_command`, `read_file`, `write_file`, or `list_directory`. Use Pydantic AI's
`PrefixTools` and replace the default instructions when an agent needs both.

## Configuration

```python
from pydantic_ai_harness import SpriteSandbox

SpriteSandbox(
    token=None,
    sprite_name=None,
    session=None,
    base_url='https://api.sprites.dev',
    api_timeout=30.0,
    runtime=None,
    workdir=None,
    default_command_timeout=60.0,
    max_command_timeout=300.0,
    max_output_bytes=50 * 1024,
    max_output_lines=2000,
    max_read_bytes=5 * 1024 * 1024,
    instructions=None,
)
```

Set `instructions=''` to disable the default instructions, or pass custom text.
`runtime` only applies to a newly created Sprite. Connection and lifecycle
settings cannot be combined with an injected `session` because it already owns
them.

## API reference

- [Sprites documentation](https://docs.sprites.dev/)
- [Sprites Python SDK](https://github.com/superfly/sprites-py)
- [Pydantic AI capabilities](/ai/capabilities/overview/)
- [Sprite Sandbox source code](https://github.com/pydantic/pydantic-ai-harness/tree/main/pydantic_ai_harness/sprites/)

::: pydantic_ai_harness.sprites.SpriteSandbox

::: pydantic_ai_harness.sprites.SpriteSandboxSession

::: pydantic_ai_harness.sprites.SpriteSandboxExecResult

::: pydantic_ai_harness.sprites.SpriteSandboxError

::: pydantic_ai_harness.sprites.SpriteSandboxTerminalError

::: pydantic_ai_harness.sprites.SpriteSandboxAuthError

::: pydantic_ai_harness.sprites.SpriteSandboxUnavailableError

::: pydantic_ai_harness.sprites.SpriteSandboxOwnershipError
