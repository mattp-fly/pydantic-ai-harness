"""Sprites toolset: gives agents a persistent cloud computer to work in."""

from __future__ import annotations

import math
from typing import Annotated

from pydantic import Field
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.tools import AgentDepsT
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset
from typing_extensions import Self

from pydantic_ai_harness._sandbox_output import guard_read_size, render_file_window, truncate_output
from pydantic_ai_harness.sprites._session import (
    SpriteSandboxError,
    SpriteSandboxSession,
    SpriteSandboxTerminalError,
)


class SpriteSandboxToolset(FunctionToolset[AgentDepsT]):
    """Gives an agent a Sprite to run commands and manage files in."""

    def __init__(
        self,
        *,
        token: str | None,
        sprite_name: str | None,
        base_url: str,
        api_timeout: float,
        runtime: str | None,
        workdir: str | None,
        default_command_timeout: float,
        max_command_timeout: float,
        max_output_bytes: int,
        max_output_lines: int,
        max_read_bytes: int,
        session: SpriteSandboxSession | None = None,
        _run_scoped: bool = False,
    ) -> None:
        super().__init__()
        self._token = token
        self._sprite_name = sprite_name
        self._base_url = base_url
        self._api_timeout = api_timeout
        self._runtime = runtime
        self._workdir = workdir
        self._default_command_timeout = default_command_timeout
        self._max_command_timeout = max_command_timeout
        self._max_output_bytes = max_output_bytes
        self._max_output_lines = max_output_lines
        self._max_read_bytes = max_read_bytes
        self._external_session = session
        self._session: SpriteSandboxSession | None = None
        self._run_scoped = _run_scoped

        self.add_function(
            self.run_command,
            name='run_command',
            metadata={'code_arg_name': 'command', 'code_arg_language': 'shell'},
        )
        self.add_function(self.read_file, name='read_file')
        self.add_function(self.write_file, name='write_file')
        self.add_function(self.list_directory, name='list_directory')

    async def for_run(self, ctx: RunContext[AgentDepsT]) -> AbstractToolset[AgentDepsT]:
        """Return a fresh instance with one Sprite session for this agent run."""
        return SpriteSandboxToolset[AgentDepsT](
            token=self._token,
            sprite_name=self._sprite_name,
            base_url=self._base_url,
            api_timeout=self._api_timeout,
            runtime=self._runtime,
            workdir=self._workdir,
            default_command_timeout=self._default_command_timeout,
            max_command_timeout=self._max_command_timeout,
            max_output_bytes=self._max_output_bytes,
            max_output_lines=self._max_output_lines,
            max_read_bytes=self._max_read_bytes,
            session=self._external_session,
            _run_scoped=True,
        )

    async def __aenter__(self) -> Self:
        """Open a per-run Sprite, or use an already-open caller-owned session."""
        if not self._run_scoped:
            return self
        if self._external_session is not None:
            if not self._external_session.is_open:
                raise SpriteSandboxError(
                    'The injected session is not open. Enter it with `async with session:` before running the agent.'
                )
            self._session = self._external_session
            return self
        session = SpriteSandboxSession(
            token=self._token,
            sprite_name=self._sprite_name,
            base_url=self._base_url,
            api_timeout=self._api_timeout,
            runtime=self._runtime,
            workdir=self._workdir,
        )
        await session.__aenter__()
        self._session = session
        return self

    async def __aexit__(self, *args: object) -> None:
        """Close the per-run session, leaving a caller-owned session open."""
        session = self._session
        self._session = None
        if session is not None and self._external_session is None:
            await session.__aexit__(*args)

    def _require_session(self) -> SpriteSandboxSession:
        if self._session is None:
            raise SpriteSandboxError('The Sprite session is not open.')
        return self._session

    def _command_timeout(self, timeout_seconds: float | None) -> float:
        if timeout_seconds is not None and (
            type(timeout_seconds) is bool or not math.isfinite(timeout_seconds) or timeout_seconds <= 0
        ):
            raise ModelRetry(f'timeout_seconds must be greater than 0, got {timeout_seconds}.')
        requested = timeout_seconds if timeout_seconds is not None else self._default_command_timeout
        return min(requested, self._max_command_timeout)

    async def run_command(self, command: str, *, timeout_seconds: float | None = None) -> str:
        """Run a shell command in the Sprite and return its combined output.

        Args:
            command: The shell command to run.
            timeout_seconds: Maximum seconds to wait (default: the configured timeout).
        """
        session = self._require_session()
        timeout = self._command_timeout(timeout_seconds)
        try:
            result = await session.exec(command, timeout=timeout, max_output_bytes=self._max_output_bytes)
        except SpriteSandboxTerminalError:
            raise
        except SpriteSandboxError as e:
            raise ModelRetry(str(e))

        output = result.output or '(no output)'
        output = truncate_output(
            output,
            max_lines=self._max_output_lines,
            max_bytes=self._max_output_bytes,
            direction='tail',
            already_truncated=result.truncated,
        )
        if result.timed_out:
            return f'{output}\n[timed out after {result.applied_timeout}s]'
        if result.returncode:
            return f'{output}\n[exit code: {result.returncode}]'
        return output

    async def read_file(
        self,
        path: str,
        *,
        offset: Annotated[int | None, Field(description='Line number to start reading from (1-indexed)')] = None,
        limit: Annotated[int | None, Field(description='Maximum number of lines to read')] = None,
    ) -> str:
        """Read a text file from the Sprite.

        Args:
            path: Path inside the Sprite, relative to its command working directory by default.
            offset: Line number to start reading from (1-indexed).
            limit: Maximum number of lines to read.
        """
        session = self._require_session()
        try:
            guard_read_size(await session.file_size(path), max_bytes=self._max_read_bytes)
            data = await session.read_bytes(path)
        except SpriteSandboxTerminalError:
            raise
        except SpriteSandboxError as e:
            raise ModelRetry(f'Could not read {path!r}: {e}')
        guard_read_size(len(data), max_bytes=self._max_read_bytes)
        return render_file_window(
            data,
            offset=offset,
            limit=limit,
            max_lines=self._max_output_lines,
            max_bytes=self._max_output_bytes,
        )

    async def write_file(self, path: str, content: str) -> str:
        """Write text to a file in the Sprite, creating parent directories.

        Args:
            path: Path inside the Sprite, relative to its command working directory by default.
            content: The UTF-8 text to write.
        """
        session = self._require_session()
        try:
            data = content.encode('utf-8')
        except UnicodeEncodeError:
            raise ModelRetry('content contains characters that cannot be encoded as UTF-8 (unpaired surrogates).')
        try:
            await session.write_bytes(path, data)
        except SpriteSandboxTerminalError:
            raise
        except SpriteSandboxError as e:
            raise ModelRetry(f'Could not write {path!r}: {e}')
        return f'Wrote {len(data)} bytes to {path!r}.'

    async def list_directory(self, path: str = '.') -> str:
        """List entries in a Sprite directory, with `/` after directory names.

        Args:
            path: Directory to list, relative to the command working directory by default.
        """
        session = self._require_session()
        try:
            entries = await session.list_files(path)
        except SpriteSandboxTerminalError:
            raise
        except SpriteSandboxError as e:
            raise ModelRetry(f'Could not list {path!r}: {e}')
        if not entries:
            return '(empty)'
        names = [f'{name}/' if is_dir else name for name, is_dir in sorted(entries)]
        return truncate_output(
            '\n'.join(names),
            max_lines=self._max_output_lines,
            max_bytes=self._max_output_bytes,
            direction='head',
        )
