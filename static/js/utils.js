/**
 * Shared utilities for FSY Packet Console frontend.
 *
 * Consolidates duplicated helper functions from across JS modules:
 * - Time/date formatting
 * - HTML escaping (XSS prevention)
 * - Position validation
 * - Leaflet tile layer definitions
 */

// ────────────────────────────────────────────────────────────────
// Time Formatting
// ────────────────────────────────────────────────────────────────

/**
 * Format a timestamp as relative time (e.g. "5m ago", "2h ago").
 *
 * This is the canonical implementation — replaces copies in
 * map.js, charts.js, and stations-table.js.
 *
 * @param {string|Date} timestamp - ISO timestamp string or Date object
 * @returns {string} Formatted relative time string
 */
export function formatRelativeTime(timestamp) {
    if (!timestamp) return '—';

    const date = new Date(timestamp);
    const now = new Date();
    const diffMs = now - date;
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);

    if (diffMins < 1) return 'Just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays < 7) return `${diffDays}d ago`;

    // Older than a week — show compact date + time
    const month = date.getMonth() + 1;
    const day = date.getDate();
    const hours = date.getHours().toString().padStart(2, '0');
    const mins = date.getMinutes().toString().padStart(2, '0');
    return `${month}/${day} ${hours}:${mins}`;
}

// ────────────────────────────────────────────────────────────────
// HTML Safety
// ────────────────────────────────────────────────────────────────

/**
 * Escape HTML entities to prevent XSS from untrusted data
 * (e.g. APRS callsigns, comments received over RF).
 *
 * @param {string} text - Raw text to escape
 * @returns {string} HTML-safe string
 */
export function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    const div = document.createElement('div');
    div.textContent = String(text);
    return div.innerHTML;
}

// ────────────────────────────────────────────────────────────────
// Position Validation
// ────────────────────────────────────────────────────────────────

/**
 * Check that a lat/lon pair is not Null Island (0, 0) or otherwise invalid.
 *
 * @param {number} lat
 * @param {number} lon
 * @returns {boolean}
 */
export function isValidPosition(lat, lon) {
    if (lat === undefined || lon === undefined || lat === null || lon === null) return false;
    if (lat === 0 && lon === 0) return false;  // Null Island
    if (Math.abs(lat) > 90 || Math.abs(lon) > 180) return false;
    return true;
}

// ────────────────────────────────────────────────────────────────
// Leaflet Tile Layers
// ────────────────────────────────────────────────────────────────

/**
 * Create the standard set of Leaflet base tile layers.
 *
 * @returns {{ layers: Object<string, L.TileLayer>, default: L.TileLayer }}
 */
export function createBaseLayers() {
    const streetMap = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        maxZoom: 19
    });

    const topoMap = L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
        attribution: 'Map data: &copy; OpenStreetMap contributors, SRTM | Map style: &copy; OpenTopoMap (CC-BY-SA)',
        maxZoom: 17
    });

    const terrainMap = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}', {
        attribution: 'Tiles &copy; Esri',
        maxZoom: 18
    });

    const satelliteMap = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
        attribution: 'Tiles &copy; Esri',
        maxZoom: 18
    });

    return {
        layers: {
            'Street': streetMap,
            'Terrain': terrainMap,
            'Topographic': topoMap,
            'Satellite': satelliteMap,
        },
        default: streetMap,
    };
}

// ────────────────────────────────────────────────────────────────
// SSE Manager
// ────────────────────────────────────────────────────────────────

/**
 * Centralised Server-Sent Events manager with:
 * - Exponential back-off reconnection
 * - Automatic cleanup on page unload
 * - Single consistent strategy across all pages
 */
export class SSEManager {
    /**
     * @param {string} url              SSE endpoint URL
     * @param {Object}  opts
     * @param {Object.<string, function>} opts.listeners  event-name → handler map
     * @param {function} [opts.onConnected]   called on successful open
     * @param {function} [opts.onDisconnected] called when connection drops
     * @param {number}   [opts.maxDelay=30000] max reconnect delay (ms)
     */
    constructor(url, opts = {}) {
        this.url = url;
        this.listeners = opts.listeners || {};
        this.onConnected = opts.onConnected || null;
        this.onDisconnected = opts.onDisconnected || null;
        this.maxDelay = opts.maxDelay || 30000;

        this._es = null;
        this._attempt = 0;
        this._timer = null;
        this._closed = false;

        // Clean up on page unload
        this._unloadHandler = () => this.close();
        window.addEventListener('beforeunload', this._unloadHandler);

        this.connect();
    }

    connect() {
        if (this._closed) return;

        // Close existing connection
        if (this._es) {
            this._es.close();
            this._es = null;
        }

        this._es = new EventSource(this.url);

        // Register event listeners
        for (const [event, handler] of Object.entries(this.listeners)) {
            this._es.addEventListener(event, handler);
        }

        this._es.onopen = () => {
            this._attempt = 0;
            if (this.onConnected) this.onConnected();
        };

        this._es.onerror = () => {
            if (this.onDisconnected) this.onDisconnected();
            if (this._es.readyState === EventSource.CLOSED) {
                this._scheduleReconnect();
            }
        };
    }

    _scheduleReconnect() {
        if (this._closed) return;
        this._attempt++;
        const delay = Math.min(1000 * Math.pow(2, this._attempt), this.maxDelay);
        console.log(`SSE reconnecting in ${delay}ms (attempt ${this._attempt})`);
        this._timer = setTimeout(() => this.connect(), delay);
    }

    close() {
        this._closed = true;
        if (this._timer) clearTimeout(this._timer);
        if (this._es) {
            this._es.close();
            this._es = null;
        }
        window.removeEventListener('beforeunload', this._unloadHandler);
    }
}
