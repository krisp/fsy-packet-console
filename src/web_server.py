"""APRS Web Server - aiohttp-based web interface.

Provides a web UI for the APRS console with real-time updates via SSE.
"""

import asyncio
import gzip
import json
import os
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Set

import aiohttp
from aiohttp import web

from .utils import print_info
from .web_api import APIHandlers, serialize_station, serialize_weather


class WebServer:
    """Async web server for APRS console UI."""

    def __init__(self, radio, aprs_manager, get_mycall: Callable[[], str], get_mylocation: Callable[[], str] = None, get_wxtrend: Callable[[], str] = None, tnc_config=None):
        """Initialize web server.

        Args:
            radio: Radio instance (for future extensions)
            aprs_manager: APRSManager instance
            get_mycall: Callable that returns current MYCALL
            get_mylocation: Callable that returns current MYLOCATION (optional)
            get_wxtrend: Callable that returns current WXTREND threshold (optional)
            tnc_config: TNCConfig instance for POST API endpoints (optional)
        """
        self.radio = radio
        self.aprs = aprs_manager
        self.get_mycall = get_mycall
        self.get_mylocation = get_mylocation or (lambda: "")
        self.get_wxtrend = get_wxtrend or (lambda: "0.3")
        self.tnc_config = tnc_config
        self.app = None
        self.runner = None
        self.site = None
        self.sse_queues: Set[asyncio.Queue] = set()
        self.start_time = datetime.now(timezone.utc)

        # Static file directory
        self.static_dir = Path(__file__).parent.parent / 'static'

    async def start(self, host: str = '0.0.0.0', port: int = 8002) -> bool:
        """Start the web server.

        Args:
            host: Bind address (0.0.0.0 for LAN access)
            port: HTTP port

        Returns:
            True if started successfully, False otherwise
        """
        start_time = time.time()

        try:
            # Ensure dependencies are downloaded
            await self._ensure_dependencies()

            # Create aiohttp application with compression middleware
            # Compression reduces JSON response sizes by 60-80% (e.g., 100KB → 20-40KB)
            # Only compresses responses > 1KB to avoid CPU overhead on small responses
            self.app = web.Application(middlewares=[
                self._compression_middleware,
                web.normalize_path_middleware(append_slash=False),
            ])

            # Create API handlers
            # Get send_beacon callable from radio.cmd_processor if available
            send_beacon = None
            if hasattr(self.radio, 'cmd_processor') and self.radio.cmd_processor:
                send_beacon = self.radio.cmd_processor._send_position_beacon

            api_handlers = APIHandlers(
                self.aprs,
                self.get_mycall,
                self.start_time,
                self.get_mylocation,
                self.get_wxtrend,
                self.tnc_config,
                send_beacon
            )

            # Setup routes
            self.app.router.add_get('/', self._handle_index)
            self.app.router.add_get('/stations', self._handle_stations_list)
            self.app.router.add_get('/station/{callsign}', self._handle_station_detail)
            self.app.router.add_get('/weather-map', self._handle_weather_map)
            self.app.router.add_get('/digipeater-dashboard', self._handle_digipeater_dashboard)
            self.app.router.add_static('/static', self.static_dir, show_index=False)

            # API routes
            self.app.router.add_get('/api/stations', api_handlers.handle_get_stations)
            self.app.router.add_get('/api/stations/{callsign}', api_handlers.handle_get_station)
            self.app.router.add_get('/api/weather', api_handlers.handle_get_weather)
            self.app.router.add_get('/api/zambretti/{callsign}', api_handlers.handle_get_zambretti_forecast)
            self.app.router.add_get('/api/messages', api_handlers.handle_get_messages)
            self.app.router.add_get('/api/monitored_messages', api_handlers.handle_get_monitored_messages)
            self.app.router.add_get('/api/digipeaters', api_handlers.handle_get_digipeater_coverage)
            self.app.router.add_get('/api/digipeaters/{callsign}', api_handlers.handle_get_digipeater)
            self.app.router.add_get('/api/status', api_handlers.handle_get_status)
            self.app.router.add_get('/api/gps', api_handlers.handle_get_gps)
            self.app.router.add_get('/api/events', self._handle_sse)

            # Digipeater statistics API routes
            self.app.router.add_get('/api/digipeater/stats', api_handlers.get_digipeater_stats)
            self.app.router.add_get('/api/digipeater/activity', api_handlers.get_digipeater_activity)
            self.app.router.add_get('/api/digipeater/top-stations', api_handlers.get_digipeater_top_stations)
            self.app.router.add_get('/api/digipeater/path-usage', api_handlers.get_digipeater_path_usage)
            self.app.router.add_get('/api/digipeater/heatmap', api_handlers.get_digipeater_heatmap)
            self.app.router.add_get('/api/digipeater/network', api_handlers.get_network_digipeater_stats)
            self.app.router.add_get('/api/digipeater/network/path-usage', api_handlers.get_network_path_usage)
            self.app.router.add_get('/api/digipeater/network/heatmap', api_handlers.get_network_heatmap)

            # POST routes (require authentication via WEBUI_PASSWORD)
            self.app.router.add_post('/api/messages', api_handlers.handle_send_message)
            self.app.router.add_post('/api/stations/paths', api_handlers.handle_get_station_paths)
            self.app.router.add_post('/api/beacon/comment', api_handlers.handle_update_beacon_comment)

            # Start server
            server_start = time.time()
            self.runner = web.AppRunner(self.app)
            await self.runner.setup()
            self.site = web.TCPSite(self.runner, host, port)
            await self.site.start()
            server_time = time.time() - server_start

            # Register for APRS events
            self.aprs.set_web_broadcast_callback(self.broadcast_event)

            return True

        except OSError:
            # Re-raise bind errors so caller can handle them specifically
            raise
        except Exception as e:
            print_info(f"Failed to start web server: {e}")
            import traceback
            traceback.print_exc()
            return False

    async def stop(self):
        """Stop the web server gracefully."""
        # Close all SSE connections
        for queue in self.sse_queues:
            await queue.put(None)  # Signal shutdown
        self.sse_queues.clear()

        # Stop server
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()

    async def _handle_index(self, request: web.Request) -> web.Response:
        """Serve the main dashboard page."""
        index_file = self.static_dir / 'index.html'
        if not index_file.exists():
            return web.Response(text="Web UI not found. Please check static files.", status=500)

        with open(index_file, 'r') as f:
            html = f.read()

        return web.Response(text=html, content_type='text/html')

    async def _handle_stations_list(self, request: web.Request) -> web.Response:
        """Serve the stations table list page."""
        stations_file = self.static_dir / 'stations.html'
        if not stations_file.exists():
            return web.Response(text="Stations page not found. Please check static files.", status=500)

        with open(stations_file, 'r') as f:
            html = f.read()

        return web.Response(text=html, content_type='text/html')

    async def _handle_station_detail(self, request: web.Request) -> web.Response:
        """Serve the station detail page."""
        station_file = self.static_dir / 'station.html'
        if not station_file.exists():
            return web.Response(text="Station detail page not found.", status=500)

        with open(station_file, 'r') as f:
            html = f.read()

        return web.Response(text=html, content_type='text/html')

    async def _handle_weather_map(self, request: web.Request) -> web.Response:
        """Serve the weather map page."""
        weather_map_file = self.static_dir / 'weather-map.html'
        if not weather_map_file.exists():
            return web.Response(text="Weather map page not found.", status=500)

        with open(weather_map_file, 'r') as f:
            html = f.read()

        return web.Response(text=html, content_type='text/html')

    async def _handle_digipeater_dashboard(self, request: web.Request) -> web.Response:
        """Serve the digipeater statistics dashboard page."""
        dashboard_file = self.static_dir / 'digipeater-dashboard.html'
        if not dashboard_file.exists():
            return web.Response(text="Digipeater dashboard page not found. Please check static files.", status=500)

        with open(dashboard_file, 'r') as f:
            html = f.read()

        return web.Response(text=html, content_type='text/html')

    async def _handle_sse(self, request: web.Request) -> web.StreamResponse:
        """Server-Sent Events endpoint for real-time updates.

        Sends events:
            - station_update: New or updated station
            - weather_update: New weather report
            - message_received: New message
        """
        response = web.StreamResponse()
        response.headers['Content-Type'] = 'text/event-stream'
        response.headers['Cache-Control'] = 'no-cache'
        response.headers['Connection'] = 'keep-alive'

        try:
            await response.prepare(request)
        except (ConnectionResetError, aiohttp.ClientConnectionResetError):
            # Client disconnected before we could send headers - this is normal
            # (client navigated away, network issue, etc.) - exit gracefully
            return response

        # Create queue for this client
        queue = asyncio.Queue()
        self.sse_queues.add(queue)

        try:
            # Send initial connection event
            await response.write(b'event: connected\ndata: {"status":"ok"}\n\n')

            # Process events from queue
            while True:
                event = await queue.get()
                if event is None:  # Shutdown signal
                    break

                event_type, data = event

                # Format SSE event
                sse_data = f'event: {event_type}\ndata: {json.dumps(data)}\n\n'

                try:
                    await response.write(sse_data.encode('utf-8'))
                except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                    # Client disconnected (e.g., navigated away) - exit gracefully
                    break
                except Exception:
                    # Any other connection error - exit gracefully
                    break

        except asyncio.CancelledError:
            pass
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            # Client disconnected during initial write - normal, exit gracefully
            pass
        finally:
            self.sse_queues.discard(queue)

        return response

    @web.middleware
    async def _compression_middleware(self, request: web.Request, handler):
        """Middleware to compress responses with gzip/deflate.

        Compresses responses when:
        - Client sends Accept-Encoding header with gzip or deflate
        - Response is larger than 1KB (compression overhead not worth it for small responses)
        - Content-Type is compressible (JSON, HTML, CSS, JS, etc.)

        Typical compression ratios:
        - JSON: 60-80% reduction (e.g., 100KB → 20-40KB)
        - HTML: 50-70% reduction
        """
        response = await handler(request)

        # Skip compression for streaming responses (SSE)
        if isinstance(response, web.StreamResponse) and not isinstance(response, web.Response):
            return response

        # Skip if no body or body too small (< 1KB)
        if not response.body or len(response.body) < 1024:
            return response

        # Check if content type is compressible
        content_type = response.content_type.lower()
        compressible_types = ['application/json', 'text/html', 'text/css',
                             'text/javascript', 'application/javascript']
        if not any(ct in content_type for ct in compressible_types):
            return response

        # Check client Accept-Encoding header
        accept_encoding = request.headers.get('Accept-Encoding', '').lower()

        encoding = None
        compressed_body = None

        if 'gzip' in accept_encoding:
            # gzip compression (best compatibility)
            compressed_body = gzip.compress(response.body, compresslevel=6)
            encoding = 'gzip'
        elif 'deflate' in accept_encoding:
            # deflate compression (fallback)
            compressed_body = zlib.compress(response.body, level=6)
            encoding = 'deflate'

        if encoding and compressed_body:
            # Replace body with compressed version
            response.body = compressed_body
            response.headers['Content-Encoding'] = encoding
            response.headers['Content-Length'] = str(len(compressed_body))
            # Inform browser not to transform further
            response.headers['Vary'] = 'Accept-Encoding'

        return response

    async def broadcast_event(self, event_type: str, data: dict):
        """Broadcast event to all connected SSE clients.

        Args:
            event_type: Event type (station_update, weather_update, message_received)
            data: Event data dictionary
        """
        if not self.sse_queues:
            return

        event = (event_type, data)

        # Send to all connected clients
        for queue in self.sse_queues:
            try:
                await queue.put(event)
            except Exception as e:
                print_info(f"Failed to broadcast to SSE client: {e}")

    async def _ensure_dependencies(self):
        """Download required frontend libraries if missing."""
        dep_start = time.time()

        vendor_dir = self.static_dir / 'vendor'
        vendor_dir.mkdir(parents=True, exist_ok=True)

        # Download Leaflet.js if missing
        leaflet_dir = vendor_dir / 'leaflet'
        if not leaflet_dir.exists():
            print_info("Downloading Leaflet.js...")
            dl_start = time.time()
            await self._download_leaflet(leaflet_dir)
            print_info(f"  Leaflet download time: {time.time() - dl_start:.2f}s")

        # Download Chart.js if missing
        chartjs_dir = vendor_dir / 'chart.js'
        if not chartjs_dir.exists():
            print_info("Downloading Chart.js...")
            dl_start = time.time()
            await self._download_chartjs(chartjs_dir)
            print_info(f"  Chart.js download time: {time.time() - dl_start:.2f}s")

        # Download APRS symbols if missing
        symbols_file = vendor_dir / 'aprs-symbols.png'
        if not symbols_file.exists():
            print_info("Downloading APRS symbols...")
            dl_start = time.time()
            await self._download_aprs_symbols(symbols_file)
            print_info(f"  APRS symbols download time: {time.time() - dl_start:.2f}s")

        dep_time = time.time() - dep_start
        if dep_time > 0.1:  # Only print if significant
            print_info(f"Dependency check time: {dep_time:.2f}s")

    async def _download_leaflet(self, dest_dir: Path):
        """Download Leaflet.js 1.9.4 from CDN."""
        dest_dir.mkdir(parents=True, exist_ok=True)

        files = {
            'leaflet.js': 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js',
            'leaflet.css': 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
        }

        async with aiohttp.ClientSession() as session:
            for filename, url in files.items():
                try:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            content = await resp.read()
                            with open(dest_dir / filename, 'wb') as f:
                                f.write(content)
                            print_info(f"  Downloaded {filename}")
                        else:
                            print_info(f"  Failed to download {filename}: HTTP {resp.status}")
                except Exception as e:
                    print_info(f"  Error downloading {filename}: {e}")

            # Download images directory
            images_dir = dest_dir / 'images'
            images_dir.mkdir(exist_ok=True)

            image_files = [
                'marker-icon.png',
                'marker-icon-2x.png',
                'marker-shadow.png',
            ]

            for img in image_files:
                try:
                    url = f'https://unpkg.com/leaflet@1.9.4/dist/images/{img}'
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            content = await resp.read()
                            with open(images_dir / img, 'wb') as f:
                                f.write(content)
                except Exception as e:
                    print_info(f"  Error downloading {img}: {e}")

    async def _download_chartjs(self, dest_dir: Path):
        """Download Chart.js 4.x from CDN."""
        dest_dir.mkdir(parents=True, exist_ok=True)

        url = 'https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js'

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        with open(dest_dir / 'chart.min.js', 'wb') as f:
                            f.write(content)
                        print_info(f"  Downloaded chart.min.js")
                    else:
                        print_info(f"  Failed to download Chart.js: HTTP {resp.status}")
            except Exception as e:
                print_info(f"  Error downloading Chart.js: {e}")

    async def _download_aprs_symbols(self, dest_file: Path):
        """Download APRS symbol sprite sheet."""
        # Use symbols from aprs.org
        url = 'http://www.aprs.org/symbols/symbols-new.png'

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        with open(dest_file, 'wb') as f:
                            f.write(content)
                        print_info(f"  Downloaded APRS symbols")
                    else:
                        print_info(f"  Failed to download APRS symbols: HTTP {resp.status}")
            except Exception as e:
                print_info(f"  Error downloading APRS symbols: {e}")
