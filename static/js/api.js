/**
 * APRS API Client - Wrapper for REST API calls
 */

export class APRSApi {
    constructor(baseUrl = '') {
        this.baseUrl = baseUrl;
    }

    /**
     * Get all stations with optional sorting
     * @param {string} sortBy - 'last', 'name', 'packets', or 'hops'
     * @returns {Promise<Object>} Station list with count
     */
    async getStations(sortBy = 'last') {
        const res = await fetch(`${this.baseUrl}/api/stations?sort_by=${sortBy}`);
        if (!res.ok) throw new Error(`Failed to fetch stations: ${res.statusText}`);
        return res.json();
    }

    /**
     * Get detailed information for a specific station
     * @param {string} callsign - Station callsign
     * @returns {Promise<Object>} Station detail with history
     */
    async getStation(callsign) {
        const res = await fetch(`${this.baseUrl}/api/stations/${encodeURIComponent(callsign)}`);
        if (!res.ok) {
            if (res.status === 404) {
                throw new Error(`Station ${callsign} not found`);
            }
            throw new Error(`Failed to fetch station: ${res.statusText}`);
        }
        return res.json();
    }

    /**
     * Get all weather stations with optional sorting
     * @param {string} sortBy - 'last', 'name', or 'temp'
     * @returns {Promise<Object>} Weather station list with count
     */
    async getWeather(sortBy = 'last') {
        const res = await fetch(`${this.baseUrl}/api/weather?sort_by=${sortBy}`);
        if (!res.ok) throw new Error(`Failed to fetch weather: ${res.statusText}`);
        return res.json();
    }

    /**
     * Get messages addressed to our station
     * @param {boolean} unreadOnly - Only return unread messages
     * @returns {Promise<Object>} Message list with counts
     */
    async getMessages(unreadOnly = false) {
        const res = await fetch(`${this.baseUrl}/api/messages?unread_only=${unreadOnly}`);
        if (!res.ok) throw new Error(`Failed to fetch messages: ${res.statusText}`);
        return res.json();
    }

    /**
     * Get monitored APRS messages (all messages heard on network)
     * @param {number} limit - Maximum number of messages to return
     * @param {string} callsign - Optional callsign to filter messages to/from
     * @returns {Promise<Object>} Monitored message list with counts
     */
    async getMonitoredMessages(limit = 100, callsign = null) {
        // Build URL with proper parameter handling
        const actualLimit = (limit === null || limit === undefined) ? 100 : limit;
        let url = `${this.baseUrl}/api/monitored_messages?limit=${actualLimit}`;
        if (callsign) {
            url += `&callsign=${encodeURIComponent(callsign)}`;
        }
        const res = await fetch(url);
        if (!res.ok) {
            throw new Error(`Failed to fetch monitored messages: ${res.statusText}`);
        }
        return await res.json();
    }

    /**
     * Get system status
     * @returns {Promise<Object>} System status including uptime and counts
     */
    async getStatus() {
        const res = await fetch(`${this.baseUrl}/api/status`);
        if (!res.ok) throw new Error(`Failed to fetch status: ${res.statusText}`);
        return res.json();
    }

    /**
     * Get GPS position
     * @returns {Promise<Object>} GPS position data
     */
    async getGPS() {
        const res = await fetch(`${this.baseUrl}/api/gps`);
        if (!res.ok) throw new Error(`Failed to fetch GPS: ${res.statusText}`);
        return res.json();
    }

    /**
     * Connect to Server-Sent Events stream for real-time updates
     * @param {Function} onEvent - Callback function(eventType, data)
     * @param {Function} onConnectionChange - Callback function(status) where status is 'connected', 'disconnected', or 'reconnecting'
     * @returns {EventSource} EventSource instance
     */
    connectSSE(onEvent, onConnectionChange = null) {
        const eventSource = new EventSource(`${this.baseUrl}/api/events`);

        eventSource.addEventListener('connected', (e) => {
            console.log('SSE connected:', e.data);
            if (onConnectionChange) onConnectionChange('connected');
        });

        eventSource.addEventListener('station_update', (e) => {
            onEvent('station_update', JSON.parse(e.data));
        });

        eventSource.addEventListener('weather_update', (e) => {
            onEvent('weather_update', JSON.parse(e.data));
        });

        eventSource.addEventListener('message_received', (e) => {
            onEvent('message_received', JSON.parse(e.data));
        });

        eventSource.addEventListener('gps_update', (e) => {
            onEvent('gps_update', JSON.parse(e.data));
        });

        eventSource.onopen = (e) => {
            console.log('SSE connection opened');
            if (onConnectionChange) onConnectionChange('connected');
        };

        eventSource.onerror = (e) => {
            console.error('SSE error:', e);

            // Check if connection is actually closed
            if (eventSource.readyState === EventSource.CLOSED) {
                console.warn('SSE connection closed');
                if (onConnectionChange) onConnectionChange('disconnected');
            }

            onEvent('error', { message: 'SSE connection error' });
        };

        return eventSource;
    }
}
