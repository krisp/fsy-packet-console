# APRS Web UI

A modern, real-time web interface for the UV-50PRO APRS Console.

## Features

- **Interactive Map**: View all heard APRS stations on an OpenStreetMap-based map
- **Real-time Updates**: Server-Sent Events (SSE) for instant station, weather, and message updates
- **Weather Visualization**: Track temperature, humidity, wind, and pressure with Chart.js graphs
- **Station Details**: Click any station for detailed information, history, and statistics
- **Message Monitoring**: View received APRS messages in real-time
- **Responsive Design**: Works on desktop, tablet, and mobile browsers

## Quick Start

1. **Start the console** as normal:
   ```bash
   python3 main.py
   ```

2. **Access the Web UI**:
   - Local: http://localhost:8002
   - LAN: http://<raspberry-pi-ip>:8002
   - The IP address is displayed in the console on startup

3. **First Run**: Dependencies (Leaflet.js, Chart.js, APRS symbols) are auto-downloaded on first start

## Architecture

### Backend (Python)
- **src/web_server.py**: Main aiohttp server, SSE endpoint, dependency downloader
- **src/web_api.py**: REST API handlers and JSON serialization

### Frontend (JavaScript)
- **static/index.html**: Main dashboard with map and station list
- **static/station.html**: Detailed station view with weather charts
- **static/js/app.js**: Main application controller
- **static/js/api.js**: REST API client wrapper
- **static/js/map.js**: Leaflet.js integration
- **static/js/charts.js**: Chart.js weather visualization
- **static/js/stations.js**: Station list rendering

### API Endpoints

#### GET /api/stations
Get all heard stations with optional sorting.

**Query Parameters**:
- `sort_by`: `last` (default), `name`, `packets`, or `hops`

**Response**:
```json
{
  "stations": [
    {
      "callsign": "N0CALL-1",
      "first_heard": "2026-01-29T10:00:00",
      "last_heard": "2026-01-29T14:30:00",
      "has_position": true,
      "last_position": {
        "latitude": 37.7749,
        "longitude": -122.4194,
        "grid_square": "CM87",
        "altitude": 1234,
        "symbol_table": "/",
        "symbol_code": ">",
        "comment": "Mobile station"
      },
      "packets_heard": 42,
      "heard_direct": true,
      "hop_count": 0
    }
  ],
  "count": 1
}
```

#### GET /api/stations/{callsign}
Get detailed information for a specific station including full weather history.

**Response**: Same as station object above, plus:
- `weather_history`: Array of all weather reports (if available)

#### GET /api/weather
Get all stations with weather data.

**Query Parameters**:
- `sort_by`: `last` (default), `name`, or `temp`

#### GET /api/messages
Get messages addressed to our station.

**Query Parameters**:
- `unread_only`: `true` or `false` (default)

**Response**:
```json
{
  "messages": [
    {
      "timestamp": "2026-01-29T14:30:00",
      "from_call": "N0CALL-2",
      "to_call": "N0CALL-1",
      "message": "Hello world",
      "message_id": "12345",
      "direction": "received",
      "read": false
    }
  ],
  "count": 1,
  "unread_count": 1
}
```

#### GET /api/status
Get system status and statistics.

**Response**:
```json
{
  "mycall": "N0CALL-1",
  "uptime_seconds": 3600,
  "start_time": "2026-01-29T13:30:00",
  "station_count": 42,
  "message_count": 5,
  "unread_messages": 2,
  "weather_station_count": 8,
  "direct_stations": 15
}
```

#### GET /api/events
Server-Sent Events (SSE) endpoint for real-time updates.

**Event Types**:
- `station_update`: New or updated station (includes full station object)
- `weather_update`: New weather report (includes full station object)
- `message_received`: New message (includes message object)

**Example**:
```javascript
const eventSource = new EventSource('/api/events');
eventSource.addEventListener('station_update', (e) => {
  const station = JSON.parse(e.data);
  console.log('Station update:', station.callsign);
});
```

#### POST /api/messages
Send APRS message (NOT YET IMPLEMENTED).

**Status**: Returns HTTP 501 (Not Implemented)
**TODO**: Requires authentication implementation before enabling

## Configuration

### Network Binding
Default: `0.0.0.0:8002` (accessible from LAN)

To change, edit `src/console.py`:
```python
started = await radio.web_server.start(host='127.0.0.1', port=8002)
```

### Auto-Download Dependencies
Dependencies are downloaded automatically on first run:
- **Leaflet.js 1.9.4**: OpenStreetMap integration
- **Chart.js 4.4.1**: Weather charts
- **APRS Symbols**: Symbol sprite sheet

Files are saved to `static/vendor/` directory.

## Customization

### Adding New Charts
Edit `static/js/charts.js` to add new Chart.js visualizations:

```javascript
export function createRainChart(canvasId, weatherHistory) {
  // Your Chart.js configuration
}
```

### Changing Map Tiles
Edit `static/js/map.js` to use different tile providers:

```javascript
L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenTopoMap contributors',
  maxZoom: 17
}).addTo(this.map);
```

### Styling
All CSS is in `static/css/style.css`. The theme uses:
- Background: `#1a1a1a` (dark gray)
- Primary: `#00ff00` (green)
- Secondary: `#00aaff` (blue)
- Accent: `#ffa500` (orange for weather)

## Troubleshooting

### Web UI doesn't start
- Check that aiohttp is installed: `pip3 install aiohttp`
- Check port 8002 is not in use: `netstat -tuln | grep 8002`
- Check console output for error messages

### Dependencies won't download
- Ensure internet connection is available
- Check firewall allows HTTPS to unpkg.com and cdn.jsdelivr.net
- Manually download to `static/vendor/` if needed

### Map doesn't load
- Check browser console for JavaScript errors
- Verify Leaflet.js downloaded: `ls static/vendor/leaflet/`
- Check OpenStreetMap tiles are accessible

### Real-time updates not working
- Check SSE connection in browser DevTools → Network tab
- Look for `/api/events` connection (should stay open)
- Verify no proxy is buffering SSE responses

## Security Notes

- **LAN Access**: The server binds to `0.0.0.0`, allowing LAN access. To restrict to localhost only, change host to `127.0.0.1`
- **No Authentication**: Message sending is disabled pending authentication implementation
- **Read-Only**: Current implementation is view-only (monitoring)
- **No HTTPS**: Uses HTTP (not HTTPS). For remote access, use a reverse proxy with SSL

## Future Enhancements (TODO)

- [ ] Message sending with authentication
- [ ] Historical playback of station positions
- [ ] APRS digipeater path visualization
- [ ] Export data (CSV, KML, GPX)
- [ ] Mobile-responsive improvements
- [ ] Dark/light theme toggle
- [ ] User preferences (saved in localStorage)
- [ ] Audio notifications for new messages
- [ ] Filter stations by distance/symbol/etc.
- [ ] Wind rose chart (proper polar plot)

## Performance

- **SSE**: Each connected browser creates one EventSource connection
- **Auto-download**: Only runs once on first start (cached)
- **Database**: Shares the same APRS database as console (no duplication)
- **Memory**: Minimal overhead (~10-20 MB for aiohttp server)

## Browser Compatibility

Tested on:
- Chrome/Chromium 90+
- Firefox 88+
- Safari 14+
- Edge 90+

Requires:
- ES6 modules support
- EventSource (SSE) API
- Fetch API
