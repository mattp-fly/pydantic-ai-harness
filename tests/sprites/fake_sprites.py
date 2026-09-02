"""Controllable in-memory stand-in for the synchronous `sprites-py` SDK."""

from __future__ import annotations

import posixpath
import types
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class FakeSpriteError(Exception):
    pass


class FakeAuthenticationError(FakeSpriteError):
    pass


class FakeNotFoundError(FakeSpriteError):
    pass


class FakeTimeoutError(FakeSpriteError):
    pass


@dataclass
class FakeExecResult:
    stdout: bytes | None = b''
    stderr: bytes | None = b''
    returncode: int = 0


@dataclass
class RunCall:
    argv: tuple[str, ...]
    capture_output: bool
    timeout: float | None
    env: dict[str, str] | None
    cwd: str | None


@dataclass
class FakeStat:
    size: int
    is_dir: bool = False


class FakePath:
    def __init__(self, sprite: FakeSprite, path: str) -> None:
        self._sprite = sprite
        self._path = posixpath.normpath(path)

    @property
    def name(self) -> str:
        return posixpath.basename(self._path)

    def __truediv__(self, path: str) -> FakePath:
        if path.startswith('/'):
            return FakePath(self._sprite, path)
        return FakePath(self._sprite, posixpath.join(self._path, path))

    def stat(self) -> FakeStat:
        self._raise_if_configured()
        if self._path in self._sprite.directories:
            return FakeStat(size=0, is_dir=True)
        try:
            return FakeStat(size=len(self._sprite.files[self._path]))
        except KeyError as e:
            raise FakeSpriteError(f'No such file: {self._path}') from e

    def read_bytes(self) -> bytes:
        self._raise_if_configured()
        try:
            return self._sprite.files[self._path]
        except KeyError as e:
            raise FakeSpriteError(f'No such file: {self._path}') from e

    def write_bytes(self, data: bytes, *, mkdir_parents: bool = False) -> None:
        self._raise_if_configured()
        if mkdir_parents:
            parent = posixpath.dirname(self._path)
            while parent and parent != '/':
                self._sprite.directories.add(parent)
                parent = posixpath.dirname(parent)
        self._sprite.files[self._path] = data

    def iterdir(self) -> list[FakePath]:
        self._raise_if_configured()
        prefix = self._path.rstrip('/') + '/'
        names: set[str] = set()
        for candidate in [*self._sprite.files, *self._sprite.directories]:
            if candidate.startswith(prefix):
                remainder = candidate[len(prefix) :]
                if remainder:
                    names.add(remainder.split('/', 1)[0])
        return [FakePath(self._sprite, prefix + name) for name in names]

    def _raise_if_configured(self) -> None:
        if self._sprite.control.filesystem_error is not None:
            raise self._sprite.control.filesystem_error


class FakeFilesystem(FakePath):
    pass


class FakeSprite:
    def __init__(self, control: FakeSprites, name: str, labels: list[str]) -> None:
        self.control = control
        self.name = name
        self.labels = labels
        self.files: dict[str, bytes] = {}
        self.directories: set[str] = {'/', '/workspace'}
        self.run_calls: list[RunCall] = []

    def run(
        self,
        *argv: str,
        capture_output: bool = False,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        **kwargs: Any,
    ) -> FakeExecResult:
        if kwargs:
            raise AssertionError(f'Unexpected Sprite.run kwargs: {kwargs}')
        self.run_calls.append(RunCall(argv, capture_output, timeout, env, cwd))
        if self.control.run_error is not None:
            raise self.control.run_error
        if argv == ('pwd',):
            return self.control.pwd_result
        return self.control.responder(self.run_calls[-1])

    def filesystem(self, working_dir: str = '/') -> FakeFilesystem:
        return FakeFilesystem(self, working_dir)


class FakeClient:
    def __init__(self, control: FakeSprites, *, token: str, base_url: str, timeout: float) -> None:
        self.control = control
        self.token = token
        self.base_url = base_url
        self.timeout = timeout
        self.closed = False
        control.clients.append(self)

    def create_sprite(
        self,
        name: str,
        *,
        labels: list[str],
        runtime: str | None,
    ) -> FakeSprite:
        self.control.create_calls.append({'name': name, 'labels': labels, 'runtime': runtime})
        if self.control.create_error is not None:
            raise self.control.create_error
        retained = labels if self.control.retain_labels else []
        sprite = FakeSprite(self.control, name, retained)
        self.control.sprites[name] = sprite
        return sprite

    def get_sprite(self, name: str) -> FakeSprite:
        self.control.get_calls.append(name)
        if self.control.get_error is not None:
            raise self.control.get_error
        try:
            return self.control.sprites[name]
        except KeyError as e:
            raise FakeNotFoundError(name) from e

    def destroy_sprite(self, name: str) -> None:
        self.control.destroy_calls.append(name)
        if self.control.destroy_error is not None:
            raise self.control.destroy_error
        self.control.sprites.pop(name, None)

    def close(self) -> None:
        if self.control.close_error is not None:
            raise self.control.close_error
        self.closed = True
        self.control.close_calls += 1


class FakeSprites:
    """Control surface plus a module object injected as `sprites`."""

    def __init__(self) -> None:
        self.clients: list[FakeClient] = []
        self.sprites: dict[str, FakeSprite] = {}
        self.create_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self.destroy_calls: list[str] = []
        self.close_calls = 0
        self.create_error: Exception | None = None
        self.get_error: Exception | None = None
        self.destroy_error: Exception | None = None
        self.close_error: Exception | None = None
        self.run_error: Exception | None = None
        self.filesystem_error: Exception | None = None
        self.retain_labels = True
        self.pwd_result = FakeExecResult(stdout=b'/workspace\n')
        self.responder: Callable[[RunCall], FakeExecResult] = lambda call: FakeExecResult(stdout=b'ok\n')
        self.module, self.exceptions_module = self._build_modules()

    def add_sprite(self, name: str, *, labels: list[str] | None = None) -> FakeSprite:
        sprite = FakeSprite(self, name, labels or [])
        self.sprites[name] = sprite
        return sprite

    def _build_modules(self) -> tuple[types.ModuleType, types.ModuleType]:
        module = types.ModuleType('sprites')
        exceptions = types.ModuleType('sprites.exceptions')

        def client_factory(**kwargs: Any) -> FakeClient:
            return FakeClient(self, **kwargs)

        setattr(module, 'SpritesClient', client_factory)
        setattr(module, 'Sprite', FakeSprite)
        setattr(module, 'SpriteError', FakeSpriteError)
        setattr(module, 'AuthenticationError', FakeAuthenticationError)
        setattr(module, 'NotFoundError', FakeNotFoundError)
        setattr(exceptions, 'TimeoutError', FakeTimeoutError)
        setattr(module, 'exceptions', exceptions)
        return module, exceptions
