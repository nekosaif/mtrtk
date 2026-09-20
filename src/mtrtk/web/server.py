"""Runs uvicorn inside the daemon's event loop without stealing its signals or its process."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections.abc import Iterator

import uvicorn
from fastapi import FastAPI

from mtrtk.core.exposure import url_host

log = logging.getLogger(__name__)

STARTUP_POLL_S = 0.01  # how often `serve` looks at uvicorn's own `started` flag
SHUTDOWN_TIMEOUT_S = 10.0  # long enough for the lifespan to release the bus subscribers
LISTEN_BACKLOG = 2048  # uvicorn's own default


class _Server(uvicorn.Server):
    """uvicorn with its signal handling taken out.

    `uvicorn.Server.serve()` installs handlers for SIGINT and SIGTERM on the running loop, which
    would replace the daemon's - the receiver, the caster and the raw logger would then never
    hear the signal that is shutting the process down.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        """uvicorn answers a failed lifespan startup with `sys.exit(STARTUP_FAILURE)`.

        Raised inside the task that serves, that `SystemExit` is a `BaseException`: the consumer
        supervisor's `except Exception` never sees it and the event loop tears the whole daemon
        down - receiver, caster and all - because one of the API's bus subscribers could not be
        built. An ordinary error is something the supervisor can back off from and retry.
        """
        try:
            await super().startup(sockets=sockets)
        except SystemExit as exc:
            raise RuntimeError("web lifespan startup failed") from exc


def listen_socket(host: str, port: int) -> socket.socket:
    """Bind the listening socket ourselves, so a taken port raises instead of exiting.

    uvicorn's `Config.bind_socket()` logs the error and calls `sys.exit(1)`. Raised inside the
    task that serves, the resulting `SystemExit` is re-raised by the event loop itself rather
    than handed to whoever is awaiting it: the whole daemon would go down - receiver, caster and
    all - because the web port happened to be busy. Binding here turns that into an `OSError`
    the consumer supervisor can back off from and retry.

    `port=0` binds an ephemeral port, which is why the bound port is read back off the socket.
    """
    family, socktype, proto, _, addr = socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
    )[0]
    sock = socket.socket(family, socktype, proto)
    try:
        # Without SO_REUSEADDR a restart within the TIME_WAIT window cannot rebind the port,
        # which is exactly the moment a supervisor restarts the daemon.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(addr)
        sock.listen(LISTEN_BACKLOG)
        sock.set_inheritable(True)
    except BaseException:
        sock.close()
        raise
    return sock


class WebServer:
    """One uvicorn server, started and stopped by the daemon that owns the event loop."""

    def __init__(self, app: FastAPI, host: str, port: int, log_level: str = "warning") -> None:
        self.host = host
        self.app = app  # kept for `_release_subscribers`: what the lifespan hangs off
        # `ws="auto"` picks the sans-io websockets implementation; the older `ws="websockets"`
        # one is deprecated in uvicorn 0.53 and warns on import. `lifespan="on"` is required,
        # not optional: the hub, the system cache and the log-index mirror are built there, and
        # a startup that silently skipped them would serve an API with no live data behind it.
        self._config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level=log_level,
            access_log=False,
            lifespan="on",
            ws="auto",
        )
        self._server = _Server(self._config)
        self._socket: socket.socket | None = None
        self._bound_port: int | None = None
        self.started = asyncio.Event()

    @property
    def port(self) -> int:
        """The port being served: the configured one, or what the kernel gave `WEB_PORT=0`.

        Read off the socket once, at bind time - uvicorn closes the listener on shutdown, and
        `getsockname()` on a closed socket raises.
        """
        return self._bound_port if self._bound_port is not None else int(self._config.port)

    def bind(self) -> None:
        """Take the port. Raises `OSError` when it is not available."""
        if self._socket is None:
            self._socket = listen_socket(self.host, int(self._config.port))
            self._bound_port = int(self._socket.getsockname()[1])

    async def serve(self, stop: asyncio.Event) -> None:
        """Serve until `stop` is set, then wait for uvicorn to finish its own shutdown."""
        self.bind()
        sock = self._socket
        assert sock is not None  # `bind()` either set it or raised
        serve_task = asyncio.create_task(self._server.serve(sockets=[sock]), name="uvicorn")
        try:
            # uvicorn signals "serving" with a plain attribute rather than an event, so there is
            # nothing to await here; the poll ends on the first tick after startup either way.
            while not self._server.started and not serve_task.done():  # noqa: ASYNC110
                await asyncio.sleep(STARTUP_POLL_S)
            if serve_task.done():
                serve_task.result()  # re-raises whatever startup failed with
                raise RuntimeError("the web server stopped before it began serving")
            self.started.set()
            log.info("web UI/API listening on http://%s:%d", url_host(self.host), self.port)
            # Watch uvicorn as well as `stop`. A server that dies *while* serving leaves nothing
            # listening, and the supervisor is watching this coroutine rather than that task: on
            # `stop.wait()` alone the daemon went on believing its web consumer was fine.
            stop_task = asyncio.create_task(stop.wait(), name="web-stop")
            try:
                done, _ = await asyncio.wait(
                    {stop_task, serve_task}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                stop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stop_task
            if serve_task in done:
                serve_task.result()  # re-raises whatever ended it
                raise RuntimeError("the web server stopped while it was serving")
        finally:
            self.started.clear()
            # uvicorn closes open WebSocket connections and waits for them before it runs the
            # lifespan shutdown, so the hub's subscribers are released after its clients are
            # gone - `WsHub._hang_up` stays as the belt-and-braces half of that.
            self._server.should_exit = True
            try:
                # Already done on the paths above, where its result is what is being raised.
                if not serve_task.done():
                    await asyncio.wait_for(serve_task, SHUTDOWN_TIMEOUT_S)
            except TimeoutError:
                hub = getattr(self.app.state, "ws_hub", None)
                log.warning(
                    "the web server did not stop within %.0fs (%s websocket client(s) still "
                    "connected); releasing its bus subscriptions by hand",
                    SHUTDOWN_TIMEOUT_S,
                    getattr(hub, "client_count", "?"),
                )
            finally:
                # Unconditional, not only after a timeout: uvicorn runs the lifespan shutdown
                # itself only when its own `serve()` completes. A task that raised - or one that
                # returned before it ever started serving - leaves all three subscribed, and
                # `Daemon._run_web` builds a fresh app for every supervised attempt, so each
                # failure would add three permanent subscriptions and two live tasks. On the
                # ordinary path the lifespan has already cleared the slots and this does nothing.
                await self._release_subscribers()
                sock, self._socket = self._socket, None
                if sock is not None:
                    with contextlib.suppress(OSError):  # uvicorn closed it first, normally
                        sock.close()

    async def _release_subscribers(self) -> None:
        """Close whatever the app's lifespan still holds, in the reverse order it built them.

        A no-op once the lifespan has run its own shutdown - it clears the three `app.state`
        slots on the way out. Each is cleared here before it is closed too, so a request racing
        this sees `None` rather than a half-closed object, and one that raises cannot strand the
        next.
        """
        for name in ("log_index", "ws_hub", "system_cache"):
            subscriber = getattr(self.app.state, name, None)
            if subscriber is None:
                continue
            setattr(self.app.state, name, None)
            try:
                await subscriber.aclose()
            except Exception:  # one that will not close must not strand the other two
                log.warning("could not release %s at shutdown", name, exc_info=True)
