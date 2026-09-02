"""Lifecycle and I/O for one Fly.io Sprite."""

from __future__ import annotations

import logging
import math
import os
import shlex
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

import anyio
import anyio.lowlevel
import anyio.to_thread
from typing_extensions import Self

if TYPE_CHECKING:
    from sprites import Sprite, SpritesClient

# External assumptions verified 2026-09-02:
# - `sprites-py` authenticates with a token, creates and destroys Sprites by name, and accepts
#   labels on creation: https://github.com/superfly/sprites-py/blob/main/src/sprites/client.py
# - Commands accept argv, cwd, env, and a client-side timeout:
#   https://github.com/superfly/sprites-py/blob/main/src/sprites/sprite.py
# - Filesystem paths support stat, byte reads and writes, and directory iteration:
#   https://github.com/superfly/sprites-py/blob/main/src/sprites/filesystem.py
# Re-check those SDK surfaces before changing lifecycle, timeout, or filesystem handling.

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = 'https://api.sprites.dev'
DEFAULT_API_TIMEOUT = 30.0
DEFAULT_MAX_COMMAND_TIMEOUT = 300.0

_PROVIDER_LABEL = 'pydantic-ai-harness'
_COMMAND_ENV = 'PYDANTIC_AI_SPRITE_COMMAND'
_COMMAND_TIMEOUT_ENV = 'PYDANTIC_AI_SPRITE_TIMEOUT_SECONDS'
_TRUNCATION_MARKER = b'\n[... Sprite command output truncated ...]\n'
_TIMEOUT_MARKER = b'\n__PYDANTIC_AI_SPRITE_TIMEOUT__\n'
_TRANSPORT_TIMEOUT_GRACE = 5.0
_PWD_TIMEOUT = 10.0

_MISSING_SPRITES = (
    'The \'sprites-py\' package is required for SpriteSandbox. Install it with `uv add "pydantic-ai-harness[sprites]"`.'
)
_AUTH_MESSAGE = 'Fly.io Sprites rejected the credentials. Set SPRITE_TOKEN or pass `token=`.'

T = TypeVar('T')


async def _run_sync(func: Callable[[], T]) -> T:
    """Run one synchronous SDK call without blocking the agent event loop."""
    return await anyio.to_thread.run_sync(func)


class SpriteSandboxError(RuntimeError):
    """Base class for failures reported by the Fly.io Sprites integration."""


class SpriteSandboxTerminalError(SpriteSandboxError):
    """A Sprite failure that retrying the same tool call cannot fix."""


class SpriteSandboxUnavailableError(SpriteSandboxTerminalError):
    """The Sprite was destroyed or is otherwise no longer available."""


class SpriteSandboxAuthError(SpriteSandboxTerminalError):
    """The Fly.io Sprites API rejected the configured token."""


class SpriteSandboxOwnershipError(SpriteSandboxTerminalError):
    """Cleanup stopped because the owned Sprite's ownership label changed."""


@dataclass(frozen=True, kw_only=True)
class SpriteSandboxExecResult:
    """The outcome of running a command in a Sprite."""

    output: str
    """Combined standard output and error, decoded as UTF-8 with replacement."""
    returncode: int
    """The process exit status."""
    truncated: bool = False
    """Whether the in-Sprite output reader dropped bytes to enforce its limit."""
    timed_out: bool = False
    """Whether the in-Sprite deadline terminated the process group."""
    applied_timeout: float | None = None
    """The command deadline enforced inside the Sprite, if any."""


def _execute_wrapper(max_output_bytes: int) -> str:
    """Build an in-Sprite process-group runner with bounded combined output."""
    content_budget = max(0, max_output_bytes - len(_TRUNCATION_MARKER))
    script = f"""
import os
import signal
import subprocess
import sys
import threading

budget = {content_budget}
head_budget = budget // 2
tail_budget = budget - head_budget
truncation_marker = {_TRUNCATION_MARKER!r}
timeout_marker = {_TIMEOUT_MARKER!r}
command = os.environ[{_COMMAND_ENV!r}]
raw_timeout = os.environ.get({_COMMAND_TIMEOUT_ENV!r})
command_timeout = float(raw_timeout) if raw_timeout else None
head = bytearray()
tail = bytearray()
total = 0

process = subprocess.Popen(
    ['bash', '-lc', command],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    start_new_session=True,
)
assert process.stdout is not None

def drain_output():
    global total
    while True:
        try:
            chunk = process.stdout.read(65536)
        except (OSError, ValueError):
            return
        if not chunk:
            return
        total += len(chunk)
        if len(head) < head_budget:
            take = min(head_budget - len(head), len(chunk))
            head.extend(chunk[:take])
            chunk = chunk[take:]
        if chunk and tail_budget:
            tail.extend(chunk)
            if len(tail) > tail_budget:
                del tail[:-tail_budget]

reader = threading.Thread(target=drain_output, daemon=True)
reader.start()
timed_out = False
try:
    returncode = process.wait(timeout=command_timeout)
except subprocess.TimeoutExpired:
    timed_out = True
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        returncode = process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        returncode = process.wait()

reader.join(timeout=1.0)
if reader.is_alive():
    process.stdout.close()
    reader.join(timeout=1.0)

if total > budget:
    output = bytes(head) + truncation_marker + bytes(tail)
else:
    output = bytes(head) + bytes(tail)
sys.stdout.buffer.write(output)
if timed_out:
    sys.stdout.buffer.write(timeout_marker)
    returncode = 124
elif returncode < 0:
    returncode = 128 - returncode
sys.stdout.buffer.flush()
raise SystemExit(returncode)
""".strip()
    return f'python3 -c {shlex.quote(script)} 2>&1'


class SpriteSandboxSession:
    """Async context manager that creates or attaches to one Sprite.

    When `sprite_name` is omitted, entering the session creates a uniquely named
    Sprite with an ownership label. Exiting verifies that label before destroying
    the Sprite. When `sprite_name` is set, the session attaches to that Sprite and
    leaves it running on exit.

    Args:
        token: Fly.io Sprites API token. Defaults to `SPRITE_TOKEN` on entry.
        sprite_name: Existing Sprite to attach to. Omit to create an owned Sprite.
        base_url: Fly.io Sprites API base URL.
        api_timeout: Timeout in seconds for API calls other than Sprite creation.
        runtime: Runtime channel for an owned Sprite: `default`, `dev`, or None.
        workdir: Working directory for commands and relative file paths. When
            omitted, the session reads the Sprite's current directory on entry.
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        sprite_name: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        api_timeout: float = DEFAULT_API_TIMEOUT,
        runtime: str | None = None,
        workdir: str | None = None,
    ) -> None:
        if not base_url:
            raise ValueError('base_url must not be empty.')
        if not _is_positive_finite(api_timeout):
            raise ValueError(f'api_timeout must be a positive finite number, got {api_timeout!r}.')
        if runtime not in (None, 'default', 'dev'):
            raise ValueError(f"runtime must be 'default', 'dev', or None, got {runtime!r}.")
        if sprite_name is not None and runtime is not None:
            raise ValueError('runtime only applies when creating a Sprite; remove it when `sprite_name` is set.')

        self._token = token
        self._configured_name = sprite_name
        self._base_url = base_url
        self._api_timeout = api_timeout
        self._runtime = runtime
        self._workdir = workdir
        self._client: SpritesClient | None = None
        self._sprite: Sprite | None = None
        self._cwd: str | None = None
        self._ownership_label: str | None = None

    @property
    def sprite_name(self) -> str | None:
        """The active Sprite name, or the configured attach name before entry."""
        if self._sprite is not None:
            return self._sprite.name
        return self._configured_name

    @property
    def is_open(self) -> bool:
        """Whether the session currently has an open client and Sprite handle."""
        return self._client is not None and self._sprite is not None

    async def __aenter__(self) -> Self:
        """Create or attach to the Sprite and resolve its command working directory."""
        if self.is_open:
            raise SpriteSandboxError(
                'The session is already open; exit it before entering again. '
                'Use a separate session per concurrent context.'
            )
        try:
            from sprites import SpritesClient
        except ImportError as e:
            raise SpriteSandboxError(_MISSING_SPRITES) from e

        token = self._token or os.getenv('SPRITE_TOKEN')
        if not token:
            raise SpriteSandboxAuthError('SPRITE_TOKEN is not set. Set it or pass `token=`.')

        client = SpritesClient(token=token, base_url=self._base_url, timeout=self._api_timeout)
        self._client = client
        # Shield setup until the Sprite handle and ownership label are stored. A
        # cancellation that lands during the synchronous create call would otherwise
        # discard its return value and orphan a newly created Sprite.
        with anyio.CancelScope(shield=True):
            try:
                if self._configured_name is not None:
                    configured_name = self._configured_name
                    sprite = await _run_sync(lambda: client.get_sprite(configured_name))
                else:
                    nonce = uuid.uuid4().hex[:20]
                    sprite_name = f'pydantic-ai-{nonce}'
                    ownership_label = f'{_PROVIDER_LABEL}-{nonce}'
                    sprite = await _run_sync(
                        lambda: client.create_sprite(
                            sprite_name,
                            labels=[_PROVIDER_LABEL, ownership_label],
                            runtime=self._runtime,
                        )
                    )
                    if ownership_label not in sprite.labels:
                        try:
                            await _run_sync(lambda: client.destroy_sprite(sprite_name))
                        finally:
                            await _run_sync(client.close)
                            self._client = None
                        raise SpriteSandboxOwnershipError(
                            f'Created Sprite {sprite_name!r} did not retain its ownership label.'
                        )
                    self._ownership_label = ownership_label
                self._sprite = sprite
                self._cwd = self._workdir or await self._read_cwd(sprite)
            except Exception as e:
                error = e if isinstance(e, SpriteSandboxError) else self._open_error(e)
                if self._client is not None and self._sprite is None:
                    await _run_sync(client.close)
                    self._client = None
                elif self._client is not None:
                    try:
                        await self.__aexit__(None, None, None)
                    except Exception as cleanup_error:
                        raise SpriteSandboxError(f'{error} Cleanup also failed: {cleanup_error}') from e
                raise error from e

        try:
            await anyio.lowlevel.checkpoint()
        except BaseException:
            with anyio.CancelScope(shield=True):
                await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *args: object) -> None:
        """Destroy an owned Sprite after an ownership check, then close the SDK client."""
        # Teardown must finish even when the surrounding agent run is cancelled. Each
        # individual SDK request remains bounded by the client's API timeout.
        with anyio.CancelScope(shield=True):
            await self._close(*args)

    async def _close(self, *args: object) -> None:
        """Perform the teardown work inside `__aexit__`'s cancellation shield."""
        client = self._client
        sprite = self._sprite
        if client is None or sprite is None:
            return

        cleanup_error: BaseException | None = None
        try:
            if self._ownership_label is not None:
                await self._destroy_owned(client, sprite.name, self._ownership_label)
        except BaseException as e:
            cleanup_error = e
        finally:
            try:
                await _run_sync(client.close)
            except BaseException as e:
                cleanup_error = cleanup_error or e
            self._client = None

        if cleanup_error is None:
            self._sprite = None
            self._cwd = None
            self._ownership_label = None
            return

        body_failed = bool(args and args[0] is not None)
        if body_failed:
            logger.error(
                'Failed to clean up Sprite %r while another exception was unwinding',
                sprite.name,
                exc_info=(type(cleanup_error), cleanup_error, cleanup_error.__traceback__),
            )
            return
        raise cleanup_error

    async def _read_cwd(self, sprite: Sprite) -> str:
        result = await _run_sync(lambda: sprite.run('pwd', capture_output=True, timeout=_PWD_TIMEOUT))
        if result.returncode != 0 or not result.stdout:
            raise SpriteSandboxError(f'Could not determine the Sprite working directory (exit {result.returncode}).')
        return result.stdout.decode('utf-8', errors='replace').strip()

    async def _destroy_owned(self, client: SpritesClient, sprite_name: str, ownership_label: str) -> None:
        from sprites import NotFoundError

        try:
            current = await _run_sync(lambda: client.get_sprite(sprite_name))
        except NotFoundError:
            return
        except Exception as e:
            raise await self._operation_error(e, f'Could not verify owned Sprite {sprite_name!r}') from e
        if ownership_label not in current.labels:
            raise SpriteSandboxOwnershipError(
                f'Refusing to destroy Sprite {sprite_name!r}: its ownership label changed. '
                'Delete it manually after verifying ownership.'
            )
        try:
            await _run_sync(lambda: client.destroy_sprite(sprite_name))
        except NotFoundError:
            return
        except Exception as e:
            raise await self._operation_error(e, f'Could not destroy owned Sprite {sprite_name!r}') from e

    def _open_error(self, e: BaseException) -> SpriteSandboxError:
        from sprites import AuthenticationError, NotFoundError

        if isinstance(e, AuthenticationError):
            return SpriteSandboxAuthError(_AUTH_MESSAGE)
        if self._configured_name is not None and isinstance(e, NotFoundError):
            return SpriteSandboxUnavailableError(
                f'Could not attach to Sprite {self._configured_name!r}: it does not exist.'
            )
        return SpriteSandboxError(f'Could not start Sprite sandbox: {e}')

    def _require_open(self) -> tuple[SpritesClient, Sprite, str]:
        if self._client is None or self._sprite is None or self._cwd is None:
            raise SpriteSandboxError('The Sprite session is not open; use it as an async context manager.')
        return self._client, self._sprite, self._cwd

    async def _operation_error(self, e: BaseException, message: str) -> SpriteSandboxError:
        from sprites import AuthenticationError, NotFoundError, SpriteError

        if isinstance(e, AuthenticationError):
            return SpriteSandboxAuthError(_AUTH_MESSAGE)
        if isinstance(e, NotFoundError):
            return SpriteSandboxUnavailableError(f'{message}: the Sprite no longer exists.')
        if isinstance(e, SpriteError) and self._client is not None and self._sprite is not None:
            client = self._client
            sprite_name = self._sprite.name
            try:
                await _run_sync(lambda: client.get_sprite(sprite_name))
            except AuthenticationError:
                return SpriteSandboxAuthError(_AUTH_MESSAGE)
            except NotFoundError:
                return SpriteSandboxUnavailableError(f'{message}: Sprite {sprite_name!r} no longer exists.')
            except SpriteError:
                pass
        return SpriteSandboxError(f'{message}: {e}')

    async def exec(
        self,
        command: str,
        *,
        timeout: float | None,
        max_output_bytes: int,
    ) -> SpriteSandboxExecResult:
        """Run a shell command with bounded combined output and process-group timeout."""
        _, sprite, cwd = self._require_open()
        if timeout is not None and (type(timeout) is bool or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError(f'timeout must be a positive finite number or None, got {timeout!r}.')
        if type(max_output_bytes) is not int or max_output_bytes <= 0:
            raise ValueError(f'max_output_bytes must be a positive integer, got {max_output_bytes!r}.')

        from sprites.exceptions import TimeoutError as SpriteTimeoutError

        environment = {_COMMAND_ENV: command}
        transport_timeout: float | None = None
        if timeout is not None:
            environment[_COMMAND_TIMEOUT_ENV] = str(timeout)
            transport_timeout = timeout + _TRANSPORT_TIMEOUT_GRACE
        try:
            result = await _run_sync(
                lambda: sprite.run(
                    'bash',
                    '-lc',
                    _execute_wrapper(max_output_bytes),
                    capture_output=True,
                    timeout=transport_timeout,
                    env=environment,
                    cwd=cwd,
                )
            )
        except Exception as e:
            if isinstance(e, SpriteTimeoutError):
                return SpriteSandboxExecResult(output='', returncode=124, timed_out=True, applied_timeout=timeout)
            raise await self._operation_error(e, 'Command could not run in the Sprite') from e

        raw = (result.stdout or b'') + (result.stderr or b'')
        timed_out = raw.endswith(_TIMEOUT_MARKER)
        if timed_out:
            raw = raw[: -len(_TIMEOUT_MARKER)]
        return SpriteSandboxExecResult(
            output=raw.decode('utf-8', errors='replace'),
            returncode=result.returncode,
            truncated=_TRUNCATION_MARKER in raw,
            timed_out=timed_out,
            applied_timeout=timeout,
        )

    async def file_size(self, path: str) -> int:
        """Return a file size without reading the file."""
        _, sprite, cwd = self._require_open()
        try:
            return await _run_sync(lambda: (sprite.filesystem(cwd) / path).stat().size)
        except Exception as e:
            raise await self._operation_error(e, f'Could not stat {path!r}') from e

    async def read_bytes(self, path: str) -> bytes:
        """Read a file as bytes."""
        _, sprite, cwd = self._require_open()
        try:
            return await _run_sync(lambda: (sprite.filesystem(cwd) / path).read_bytes())
        except Exception as e:
            raise await self._operation_error(e, f'Could not read {path!r}') from e

    async def write_bytes(self, path: str, data: bytes) -> None:
        """Write bytes to a file, creating parent directories."""
        _, sprite, cwd = self._require_open()
        try:
            await _run_sync(lambda: (sprite.filesystem(cwd) / path).write_bytes(data, mkdir_parents=True))
        except Exception as e:
            raise await self._operation_error(e, f'Could not write {path!r}') from e

    async def list_files(self, path: str) -> list[tuple[str, bool]]:
        """List a directory as `(name, is_dir)` pairs."""
        _, sprite, cwd = self._require_open()

        def list_sync() -> list[tuple[str, bool]]:
            entries = (sprite.filesystem(cwd) / path).iterdir()
            return [(entry.name, entry.stat().is_dir) for entry in entries]

        try:
            return await _run_sync(list_sync)
        except Exception as e:
            raise await self._operation_error(e, f'Could not list {path!r}') from e


def _is_positive_finite(value: float) -> bool:
    """Whether a value is a positive finite int or float, excluding bool."""
    if type(value) not in (int, float):
        return False
    return math.isfinite(value) and value > 0
