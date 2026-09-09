#!/usr/bin/env python3
"""
RoadShield — jetson_ws_client.py
=================================
WebSocket client for real-time communication between the Jetson and the
Laravel control-room server.

Features:
  • Event-based message routing — each incoming message has an "event" type
    that is dispatched to a registered handler.
  • Auto-reconnect with exponential backoff (1s → 2s → 4s … 30s max).
  • Heartbeat every 30 seconds to keep the connection alive.
  • Polling fallback — when WebSocket is disconnected, the client can run
    HTTP polling callbacks so the system keeps working.

Usage:
    client = JetsonWSClient(
        ws_url="wss://controlroom.roadshield.ae/ws/surveillance",
        token="...",
        cameras=["cam1", "cam2", "cam3"],
    )
    client.on("ptz.command",     my_ptz_handler)
    client.on("settings.update", my_settings_handler)

    # Optional: register a callable that runs when WS is disconnected
    client.set_fallback_poll(my_polling_function)

    client.run()          # blocking — runs the async event loop
    # or
    await client.start()  # if you already have an event loop
"""

import asyncio
import json
import logging
import time
import threading
from typing import Callable, Any

try:
    import websockets
    import websockets.exceptions
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False

import requests  # used for polling fallback

log = logging.getLogger("jetson-ws")

# ─── Constants ──────────────────────────────────────────────────────────────
HEARTBEAT_INTERVAL   = 30    # seconds
RECONNECT_MIN_DELAY  = 1     # seconds
RECONNECT_MAX_DELAY  = 30    # seconds
RECONNECT_MULTIPLIER = 2     # exponential backoff multiplier


class JetsonWSClient:
    """
    Async WebSocket client that connects to the Laravel server and routes
    incoming events to registered handlers.

    The client is fully self-contained: call `run()` to start a blocking
    event loop, or `await start()` from an existing loop.
    """

    def __init__(
        self,
        ws_url: str,
        token: str,
        cameras: list[str] | None = None,
        device_name: str | None = None,
        poll_interval: float = 0.8,
    ):
        self.ws_url = ws_url
        self.token = token
        self.cameras = cameras or []
        self.device_name = device_name or "jetson-1"
        self.poll_interval = poll_interval

        self._handlers: dict[str, Callable] = {}
        self._fallback_poll: Callable | None = None
        self._ws = None
        self._connected = False
        self._shutdown = False
        self._reconnect_delay = RECONNECT_MIN_DELAY

    # ─── Event registration ─────────────────────────────────────────────

    def on(self, event_type: str, handler: Callable) -> None:
        """
        Register a handler for an event type.

        The handler receives (event_data: dict) and may return a dict
        that will be sent back to the server as an acknowledgement.
        """
        self._handlers[event_type] = handler
        log.debug(f"Registered handler for event: {event_type}")

    def set_fallback_poll(self, poll_fn: Callable) -> None:
        """
        Set a synchronous callable that will be invoked in a loop when the
        WebSocket is disconnected.  This keeps the system operational while
        we try to reconnect.

        poll_fn() should execute one polling cycle (all cameras) and return.
        """
        self._fallback_poll = poll_fn

    # ─── Public API ─────────────────────────────────────────────────────

    def run(self) -> None:
        """Start the client (blocking).  Creates a new event loop."""
        if not HAS_WEBSOCKETS:
            log.warning(
                "websockets library not installed. "
                "Running in polling-only mode. "
                "Install with: pip install websockets"
            )
            self._run_polling_only()
            return

        try:
            asyncio.run(self.start())
        except KeyboardInterrupt:
            log.info("Interrupted by user.")

    async def start(self) -> None:
        """Start the client (async).  Must be awaited."""
        self._shutdown = False
        log.info("WebSocket client starting...")
        log.info(f"  Server : {self.ws_url}")
        log.info(f"  Cameras: {', '.join(self.cameras)}")
        log.info(f"  Events : {', '.join(self._handlers.keys()) or '(none)'}")

        while not self._shutdown:
            try:
                await self._connect_and_listen()
            except Exception as e:
                if self._shutdown:
                    break
                log.warning(f"Connection lost: {e}")
                await self._handle_reconnect()

        log.info("WebSocket client stopped.")

    def stop(self) -> None:
        """Signal the client to shut down gracefully."""
        self._shutdown = True
        if self._ws:
            asyncio.ensure_future(self._ws.close())

    @property
    def connected(self) -> bool:
        return self._connected

    # ─── Send ────────────────────────────────────────────────────────────

    async def send(self, event: str, data: dict | None = None) -> None:
        """Send an event message to the server."""
        if not self._ws or not self._connected:
            log.warning(f"Cannot send '{event}' — not connected.")
            return

        message = json.dumps({"event": event, "data": data or {}})
        try:
            await self._ws.send(message)
            log.debug(f"Sent: {event}")
        except Exception as e:
            log.warning(f"Send failed for '{event}': {e}")

    def send_sync(self, event: str, data: dict | None = None) -> None:
        """Thread-safe synchronous wrapper around send()."""
        if not self._ws or not self._connected:
            return
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self.send(event, data))
            else:
                loop.run_until_complete(self.send(event, data))
        except RuntimeError:
            pass

    # ─── Core connection logic ───────────────────────────────────────────

    async def _connect_and_listen(self) -> None:
        """Open a WebSocket connection and listen for events."""
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Cameras": ",".join(self.cameras),
        }

        log.info(f"Connecting to {self.ws_url} ...")

        async with websockets.connect(
            self.ws_url,
            additional_headers=headers,
            ping_interval=HEARTBEAT_INTERVAL,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            self._connected = True
            self._reconnect_delay = RECONNECT_MIN_DELAY  # reset backoff
            log.info("✅ WebSocket connected!")

            # Send initial identification
            await self.send("jetson.hello", {
                "cameras": self.cameras,
                "jetson_name": self.device_name,
                "version": "2.0.0",
                "timestamp": time.time(),
            })

            # Start heartbeat task
            heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            try:
                async for raw_message in ws:
                    await self._dispatch(raw_message)
            finally:
                heartbeat_task.cancel()
                self._connected = False
                self._ws = None

    async def _dispatch(self, raw_message: str) -> None:
        """Parse an incoming message and route it to the correct handler."""
        try:
            message = json.loads(raw_message)
        except json.JSONDecodeError:
            log.warning(f"Invalid JSON received: {raw_message[:100]}")
            return

        event_type = message.get("event")
        event_data = message.get("data", {})

        if not event_type:
            log.warning(f"Message without 'event' field: {raw_message[:100]}")
            return

        handler = self._handlers.get(event_type)
        if handler is None:
            log.debug(f"No handler for event: {event_type}")
            return

        log.info(f"← Received event: {event_type}")

        try:
            result = handler(event_data)

            # If the handler returns a dict, send it back as acknowledgement
            if isinstance(result, dict):
                ack_event = f"{event_type}.ack"
                await self.send(ack_event, result)

        except Exception as e:
            log.error(f"Handler error for '{event_type}': {e}", exc_info=True)
            await self.send(f"{event_type}.error", {
                "error": str(e),
                "event": event_type,
            })

    # ─── Heartbeat ───────────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeats to keep the connection alive."""
        while self._connected and not self._shutdown:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if self._connected:
                    await self.send("heartbeat", {"timestamp": time.time()})
            except asyncio.CancelledError:
                break
            except Exception:
                break

    # ─── Reconnect with backoff ──────────────────────────────────────────

    async def _handle_reconnect(self) -> None:
        """Wait with exponential backoff, running polling fallback if set."""
        self._connected = False
        delay = self._reconnect_delay

        log.info(f"Reconnecting in {delay}s...")

        if self._fallback_poll:
            log.info("Running polling fallback while disconnected...")
            # Run polling in a thread so we don't block the async loop
            end_time = time.time() + delay
            poll_thread = threading.Thread(
                target=self._run_fallback_during_delay,
                args=(end_time,),
                daemon=True,
            )
            poll_thread.start()
            await asyncio.sleep(delay)
            poll_thread.join(timeout=1)
        else:
            await asyncio.sleep(delay)

        # Increase delay for next failure (exponential backoff)
        self._reconnect_delay = min(
            delay * RECONNECT_MULTIPLIER,
            RECONNECT_MAX_DELAY,
        )

    def _run_fallback_during_delay(self, end_time: float) -> None:
        """Run the polling fallback function until end_time."""
        while time.time() < end_time and not self._shutdown:
            try:
                self._fallback_poll()
            except Exception as e:
                log.warning(f"Fallback poll error: {e}")
            time.sleep(self.poll_interval)

    # ─── Polling-only mode (no websockets library) ───────────────────────

    def _run_polling_only(self) -> None:
        """
        Fallback: if websockets is not installed, run polling forever.
        This keeps backward compatibility.
        """
        if not self._fallback_poll:
            log.error(
                "No WebSocket library and no fallback poll registered. "
                "Nothing to do."
            )
            return

        log.info("Running in polling-only mode...")
        try:
            while not self._shutdown:
                self._fallback_poll()
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            pass
