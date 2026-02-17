/**
 * APRS Console - Main application controller
 */

import { APRSApi } from './api.js';
import { APRSMap } from './map.js';
import { renderStationList, renderWeatherList, renderMessageList } from './stations.js';
import { formatRelativeTime, escapeHtml, SSEManager } from './utils.js';

class APRSApp {
    constructor() {
        this.api = new APRSApi();
        this.map = new APRSMap('map');
        this.sse = null;
        this.currentSort = 'last';
        // If user has saved map state, don't auto-fit bounds on initial load
        this.initialLoadComplete = this.hasSavedMapState();
        this.stationsData = null; // Cache stations data for efficient updates
        this.activityFeed = []; // Buffer for live activity feed
        this.maxActivityItems = 50; // Maximum items in activity feed

        // Filter state - initialize from HTML elements to match UI
        const timeFilterSelect = document.getElementById('time-filter');
        const directOnlyCheckbox = document.getElementById('direct-only-filter');
        const withPathCheckbox = document.getElementById('with-path-filter');
        const useAPRSIconsCheckbox = document.getElementById('use-aprs-icons');

        this.filters = {
            useAPRSIcons: useAPRSIconsCheckbox ? useAPRSIconsCheckbox.checked : false,
            timeRange: timeFilterSelect ? timeFilterSelect.value : 'all',
            directOnly: directOnlyCheckbox ? directOnlyCheckbox.checked : false,
            withPath: withPathCheckbox ? withPathCheckbox.checked : false,
            localCoverageOnly: false  // My Coverage (Direct) filter
        };
    }

    /**
     * Check if user has a saved map state in localStorage
     * @returns {boolean} True if saved state exists
     */
    hasSavedMapState() {
        try {
            return localStorage.getItem('aprs_map_state') !== null;
        } catch (e) {
            return false;
        }
    }

    /**
     * Initialize the application
     */
    async init() {
        console.log('Initializing APRS Console...');

        try {
            // Load initial data
            await this.loadStations();
            await this.loadWeather();
            await this.loadMessages();
            await this.updateStatus();
            await this.loadGPS();  // Load initial GPS position

            // Setup UI event listeners
            this.setupEventListeners();

            // Connect SSE for real-time updates
            this.connectRealtime();

            console.log('APRS Console initialized successfully');
        } catch (error) {
            console.error('Failed to initialize APRS Console:', error);
            this.showError('Failed to load initial data. Retrying in 5 seconds...');
            setTimeout(() => this.init(), 5000);
        }
    }

    /**
     * Setup UI event listeners
     */
    setupEventListeners() {
        // Station sort dropdown
        const sortSelect = document.getElementById('sort-by');
        if (sortSelect) {
            sortSelect.addEventListener('change', (e) => {
                this.currentSort = e.target.value;
                this.loadStations(this.currentSort, false); // Don't reset zoom when sorting
            });
        }

        // Fit bounds button
        const fitBoundsBtn = document.getElementById('fit-bounds-btn');
        if (fitBoundsBtn) {
            fitBoundsBtn.addEventListener('click', () => {
                this.map.fitBounds();
            });
        }

        // Message count click - show monitored messages modal
        const messageCount = document.getElementById('message-count');
        if (messageCount) {
            messageCount.addEventListener('click', () => {
                this.showMonitoredMessagesModal();
            });
        }

        // Clear activity feed button
        const clearActivityBtn = document.getElementById('clear-activity-btn');
        if (clearActivityBtn) {
            clearActivityBtn.addEventListener('click', () => {
                this.clearActivityFeed();
            });
        }

        // Close modal button
        const closeModalBtn = document.getElementById('close-modal-btn');
        if (closeModalBtn) {
            closeModalBtn.addEventListener('click', () => {
                this.hideModal();
            });
        }

        // Close modal on background click
        const modal = document.getElementById('monitored-messages-modal');
        if (modal) {
            modal.addEventListener('click', (e) => {
                if (e.target === modal) {
                    this.hideModal();
                }
            });
        }

        // Close modal on Escape key
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                this.hideModal();
            }
        });

        // Filter controls
        const useAPRSIconsCheckbox = document.getElementById('use-aprs-icons');
        if (useAPRSIconsCheckbox) {
            useAPRSIconsCheckbox.addEventListener('change', (e) => {
                this.filters.useAPRSIcons = e.target.checked;
                console.log(`APRS Icons ${e.target.checked ? 'enabled' : 'disabled'}`);
                this.map.setUseAPRSIcons(e.target.checked);
                console.log(`Refreshed ${Object.keys(this.map.markers).length} markers`);
            });
        }

        const showDigipeaterCoverageCheckbox = document.getElementById('show-digipeater-coverage');
        if (showDigipeaterCoverageCheckbox) {
            showDigipeaterCoverageCheckbox.addEventListener('change', (e) => {
                console.log(`Digipeater Coverage ${e.target.checked ? 'enabled' : 'disabled'}`);
                this.map.toggleDigipeaterCoverage(e.target.checked, this.filters.timeRange);
            });
        }

        const timeFilterSelect = document.getElementById('time-filter');
        if (timeFilterSelect) {
            timeFilterSelect.addEventListener('change', async (e) => {
                this.filters.timeRange = e.target.value;
                await this.applyFilters();

                // Update coverage circles with new time filter
                const showDigipeaterCoverageCheckbox = document.getElementById('show-digipeater-coverage');
                if (showDigipeaterCoverageCheckbox && showDigipeaterCoverageCheckbox.checked) {
                    this.map.updateDigipeaterCoverage(this.filters.timeRange);
                }
                const showLocalCoverageCheckbox = document.getElementById('show-local-coverage');
                if (showLocalCoverageCheckbox && showLocalCoverageCheckbox.checked) {
                    this.map.updateLocalCoverage(this.stationsData, this.filters.timeRange);
                }

                // Re-render paths with new time filter if currently showing
                if (this.filters.withPath && this.map.showingAllPaths) {
                    const filteredStations = this.filterStations(this.stationsData);
                    await this.map.showAllPaths(filteredStations, this.filters.timeRange);
                }
            });
        }

        const directOnlyCheckbox = document.getElementById('direct-only-filter');
        if (directOnlyCheckbox) {
            directOnlyCheckbox.addEventListener('change', (e) => {
                this.filters.directOnly = e.target.checked;
                this.applyFilters();
            });
        }

        const withPathCheckbox = document.getElementById('with-path-filter');
        if (withPathCheckbox) {
            withPathCheckbox.addEventListener('change', async (e) => {
                this.filters.withPath = e.target.checked;

                if (e.target.checked) {
                    // Show paths for all filtered stations
                    await this.applyFilters();
                    const filteredStations = this.filterStations(this.stationsData);
                    await this.map.showAllPaths(filteredStations, this.filters.timeRange);
                } else {
                    // Clear all paths and show all stations
                    this.map.clearAllPaths();
                    await this.applyFilters();
                }
            });
        }

        const showLocalCoverageCheckbox = document.getElementById('show-local-coverage');
        if (showLocalCoverageCheckbox) {
            showLocalCoverageCheckbox.addEventListener('change', (e) => {
                console.log(`My Coverage (Direct) ${e.target.checked ? 'enabled' : 'disabled'}`);
                // Update filter state
                this.filters.localCoverageOnly = e.target.checked;
                // Draw coverage circle (or clear it) with time filter
                this.map.toggleLocalCoverage(e.target.checked, this.stationsData, this.filters.timeRange);
                // Apply station filters (shows only zero-hop stations when enabled)
                this.applyFilters();
            });
        }

        // Toggle filter panel collapse
        const toggleFiltersBtn = document.getElementById('toggle-filters-btn');
        const filterContent = document.getElementById('filter-content');
        if (toggleFiltersBtn && filterContent) {
            toggleFiltersBtn.addEventListener('click', () => {
                filterContent.classList.toggle('hidden');
                toggleFiltersBtn.classList.toggle('collapsed');
                toggleFiltersBtn.textContent = filterContent.classList.contains('hidden') ? '+' : '−';
            });
        }
    }

    /**
     * Load and display all stations
     * @param {string} sortBy - Sort method
     * @param {boolean} fitBounds - Whether to fit map bounds (default: only on initial load)
     */
    async loadStations(sortBy = 'last', fitBounds = null) {
        try {
            const data = await this.api.getStations(sortBy);

            // Cache stations data
            this.stationsData = data.stations;

            // Apply filters
            const filteredStations = this.filterStations(data.stations);

            // Update map with filtered stations
            filteredStations.forEach(station => {
                this.map.addOrUpdateStation(station);
            });

            // Fit bounds on initial load only (unless explicitly requested)
            const shouldFitBounds = fitBounds !== null ? fitBounds : !this.initialLoadComplete;
            if (shouldFitBounds && this.map.getMarkerCount() > 0) {
                this.map.fitBounds();
            }

            // Mark initial load as complete
            this.initialLoadComplete = true;

            // Update sidebar list with filtered stations
            renderStationList(filteredStations);

            console.log(`Loaded ${data.count} stations (${filteredStations.length} visible after filters)`);
        } catch (error) {
            console.error('Failed to load stations:', error);
            this.showError('Failed to load stations');
        }
    }

    /**
     * Sort stations array in-place
     * @param {Array} stations - Array of station objects
     * @param {string} sortBy - Sort method ('last', 'name', 'packets', 'hops')
     */
    sortStations(stations, sortBy) {
        switch (sortBy) {
            case 'name':
                stations.sort((a, b) => a.callsign.localeCompare(b.callsign));
                break;
            case 'packets':
                stations.sort((a, b) => b.packets_heard - a.packets_heard);
                break;
            case 'hops':
                const getHops = (s) => (s.hop_count !== null && s.hop_count !== 999) ? s.hop_count : 9999;
                stations.sort((a, b) => getHops(a) - getHops(b));
                break;
            case 'last':
            default:
                stations.sort((a, b) => new Date(b.last_heard) - new Date(a.last_heard));
                break;
        }
    }

    /**
     * Filter stations based on current filter settings
     * @param {Array} stations - Array of station objects
     * @returns {Array} Filtered stations
     */
    filterStations(stations) {
        let filtered = [...stations];

        // Time range filter
        if (this.filters.timeRange !== 'all') {
            const now = new Date();
            let cutoffTime;

            switch (this.filters.timeRange) {
                case '1h':
                    cutoffTime = new Date(now - 60 * 60 * 1000);
                    break;
                case '4h':
                    cutoffTime = new Date(now - 4 * 60 * 60 * 1000);
                    break;
                case '24h':
                    cutoffTime = new Date(now - 24 * 60 * 60 * 1000);
                    break;
            }

            if (cutoffTime) {
                filtered = filtered.filter(station => {
                    const lastHeard = new Date(station.last_heard);
                    return lastHeard >= cutoffTime;
                });
            }
        }

        // Heard Direct (RF) filter - show stations heard directly over RF
        // This filters to stations where heard_direct === true, meaning we received
        // their packets via RF (not via internet/iGate/third-party packets)
        // This includes both direct reception (0 hops) and digipeated packets that
        // came via RF digipeaters
        if (this.filters.directOnly) {
            filtered = filtered.filter(station => station.heard_direct === true);
            console.log(`Heard Direct (RF) filter: ${filtered.length} stations`);
        }

        // With Path filter - show only stations with position history (2+ positions)
        // This filters to stations that have moved, excluding Null Island positions
        if (this.filters.withPath) {
            filtered = filtered.filter(station => station.has_path === true);
            console.log(`With Path filter: ${filtered.length} mobile stations`);
        }

        // My Coverage (Direct) filter - show only zero-hop stations
        // This filters to stations where heard_zero_hop === true, meaning we've
        // EVER heard them with no digipeaters (true direct coverage)
        if (this.filters.localCoverageOnly) {
            filtered = filtered.filter(station => station.heard_zero_hop === true);
            console.log(`My Coverage (Direct) filter: ${filtered.length} zero-hop stations`);
        }

        return filtered;
    }

    /**
     * Apply current filters to station display
     */
    async applyFilters() {
        if (!this.stationsData) {
            return;
        }

        // Filter stations
        const filteredStations = this.filterStations(this.stationsData);

        // Clear map markers
        Object.keys(this.map.markers).forEach(callsign => {
            this.map.removeStation(callsign);
        });

        // Add filtered stations to map
        filteredStations.forEach(station => {
            this.map.addOrUpdateStation(station);
        });

        // Update sidebar list with filtered stations
        renderStationList(filteredStations);

        // Update paths if "With Path" filter is active
        if (this.filters.withPath) {
            await this.map.showAllPaths(filteredStations);
        }

        console.log(`Filtered to ${filteredStations.length} of ${this.stationsData.length} stations`);
    }

    /**
     * Load and display weather stations
     */
    async loadWeather() {
        try {
            const data = await this.api.getWeather('last');
            renderWeatherList(data.weather_stations);
            console.log(`Loaded ${data.count} weather stations`);
        } catch (error) {
            console.error('Failed to load weather:', error);
        }
    }

    /**
     * Load and display messages
     */
    async loadMessages() {
        try {
            const data = await this.api.getMessages(false);
            renderMessageList(data.messages);
            console.log(`Loaded ${data.count} messages (${data.unread_count} unread)`);
        } catch (error) {
            console.error('Failed to load messages:', error);
        }
    }

    /**
     * Update status bar
     */
    async updateStatus() {
        try {
            const status = await this.api.getStatus();

            // Update status elements
            const mycallEl = document.getElementById('mycall');
            if (mycallEl) {
                mycallEl.textContent = status.mycall || 'N0CALL';
            }

            const stationCountEl = document.getElementById('station-count');
            if (stationCountEl) {
                stationCountEl.textContent = `${status.station_count} Stations`;
            }

            const messageCountEl = document.getElementById('message-count');
            if (messageCountEl) {
                const personalCount = status.message_count;
                const monitoredCount = status.monitored_message_count || 0;
                const unreadText = status.unread_messages > 0 ? ` (${status.unread_messages} new)` : '';
                messageCountEl.textContent = `${personalCount}/${monitoredCount} Messages${unreadText}`;
                messageCountEl.title = `${personalCount} personal, ${monitoredCount} monitored - Click to view all`;
            }

        } catch (error) {
            console.error('Failed to update status:', error);
        }
    }

    /**
     * Load GPS position
     */
    async loadGPS() {
        try {
            const response = await fetch('/api/gps');
            const gpsData = await response.json();

            // Update local station marker on map
            this.map.updateLocalStation(gpsData);

            if (gpsData.locked) {
                console.log(`GPS: ${gpsData.latitude.toFixed(6)}, ${gpsData.longitude.toFixed(6)}`);
            } else {
                console.log('GPS: No lock');
            }
        } catch (error) {
            console.error('Failed to load GPS:', error);
        }
    }

    /**
     * Connect to Server-Sent Events for real-time updates
     */
    connectRealtime() {
        // Close existing connection if any
        if (this.sse) {
            this.sse.close();
            this.sse = null;
        }

        const parseEvent = (type) => (e) => {
            try {
                this.handleRealtimeUpdate(type, JSON.parse(e.data));
            } catch (err) {
                console.error(`Error parsing ${type} event:`, err);
            }
        };

        this.sse = new SSEManager('/api/events', {
            listeners: {
                station_update: parseEvent('station_update'),
                weather_update: parseEvent('weather_update'),
                message_received: parseEvent('message_received'),
                gps_update: parseEvent('gps_update'),
                connected: () => {},  // handled via onConnected
            },
            onConnected: () => this.setConnectionStatus('connected'),
            onDisconnected: () => this.setConnectionStatus('disconnected'),
        });
    }

    /**
     * Handle real-time update events
     * @param {string} type - Event type
     * @param {Object} data - Event data
     */
    handleRealtimeUpdate(type, data) {
        console.log(`Real-time update: ${type}`, data);

        switch(type) {
            case 'station_update':
                this.handleStationUpdate(data);
                break;

            case 'weather_update':
                this.handleWeatherUpdate(data);
                break;

            case 'message_received':
                this.handleMessageReceived(data);
                break;

            case 'gps_update':
                this.handleGPSUpdate(data);
                break;

            case 'error':
                this.setConnectionStatus(false);
                break;
        }
    }

    /**
     * Handle station update event
     * @param {Object} station - Updated station data
     */
    handleStationUpdate(station) {
        // Update cached stations data first
        if (this.stationsData) {
            const existingIndex = this.stationsData.findIndex(s => s.callsign === station.callsign);
            if (existingIndex >= 0) {
                // Update existing station
                this.stationsData[existingIndex] = station;
            } else {
                // Add new station
                this.stationsData.push(station);
            }

            // Re-sort based on current sort mode
            this.sortStations(this.stationsData, this.currentSort);

            // Check if station passes filters
            const filteredStations = this.filterStations([station]);
            const passesFilter = filteredStations.length > 0;

            if (passesFilter) {
                // Update map marker if station passes filters
                this.map.addOrUpdateStation(station);
            } else {
                // Remove from map if station doesn't pass filters
                this.map.removeStation(station.callsign);
            }

            // Re-render station list with filtered data
            const allFiltered = this.filterStations(this.stationsData);
            renderStationList(allFiltered);
        }

        // Add to activity feed (always show in feed, even if filtered from map)
        const posText = station.has_position ? ` (${escapeHtml(station.last_position.grid_square)})` : '';
        this.addActivityItem('station', `${escapeHtml(station.callsign)}${posText} heard`);

        // Update local coverage if enabled (in case this is a new direct station)
        const showLocalCoverageCheckbox = document.getElementById('show-local-coverage');
        if (showLocalCoverageCheckbox && showLocalCoverageCheckbox.checked) {
            this.map.updateLocalCoverage(this.stationsData, this.filters.timeRange);
        }

        // Update status
        this.updateStatus();
    }

    /**
     * Handle weather update event
     * @param {Object} weatherData - Weather data (station object with weather)
     */
    handleWeatherUpdate(weatherData) {
        // Update cached stations data if this station is in the list
        if (this.stationsData) {
            const existingIndex = this.stationsData.findIndex(s => s.callsign === weatherData.callsign);
            if (existingIndex >= 0) {
                this.stationsData[existingIndex] = weatherData;
                this.sortStations(this.stationsData, this.currentSort);

                // Check if station passes filters
                const filteredStations = this.filterStations([weatherData]);
                const passesFilter = filteredStations.length > 0;

                if (passesFilter) {
                    // Update map marker if station passes filters
                    this.map.addOrUpdateStation(weatherData);
                } else {
                    // Remove from map if station doesn't pass filters
                    this.map.removeStation(weatherData.callsign);
                }

                // Re-render station list with filtered data
                const allFiltered = this.filterStations(this.stationsData);
                renderStationList(allFiltered);
            }
        }

        // Refresh weather list (this is lightweight, just re-fetches weather stations)
        this.loadWeather();

        // Add to activity feed (always show in feed, even if filtered from map)
        const wx = weatherData.last_weather;
        let wxText = `${escapeHtml(weatherData.callsign)} weather:`;
        if (wx.temperature !== null) wxText += ` ${wx.temperature}°F`;
        if (wx.humidity !== null) wxText += ` ${wx.humidity}%`;
        if (wx.wind_speed !== null) wxText += ` Wind ${wx.wind_speed}mph`;
        this.addActivityItem('weather', wxText);

        // Update status
        this.updateStatus();
    }

    /**
     * Handle message received event
     * @param {Object} message - Message data
     */
    handleMessageReceived(message) {
        // Refresh message list
        this.loadMessages();

        // Add to activity feed
        const msgText = `${escapeHtml(message.from_call)} → ${escapeHtml(message.to_call)}: ${escapeHtml(message.message.substring(0, 40))}${message.message.length > 40 ? '...' : ''}`;
        this.addActivityItem('message', msgText);

        // Update status
        this.updateStatus();

        // Optional: Show notification or audio alert
        console.log('New message received from', message.from_call);
    }

    /**
     * Handle GPS update event
     * @param {Object} gpsData - GPS position data
     */
    handleGPSUpdate(gpsData) {
        // Update local station marker on map
        this.map.updateLocalStation(gpsData);

        // Update local coverage circle if enabled
        const showLocalCoverageCheckbox = document.getElementById('show-local-coverage');
        if (showLocalCoverageCheckbox && showLocalCoverageCheckbox.checked) {
            this.map.updateLocalCoverage(this.stationsData, this.filters.timeRange);
        }
    }

    /**
     * Set connection status indicator
     * @param {string} status - Connection state: 'connected', 'disconnected', or 'reconnecting'
     * @param {number} delay - Reconnection delay in ms (only for 'reconnecting' status)
     */
    setConnectionStatus(status, delay = 0) {
        const statusEl = document.getElementById('connection-status');
        if (!statusEl) return;

        // Clear any existing countdown interval
        if (this.statusCountdownInterval) {
            clearInterval(this.statusCountdownInterval);
            this.statusCountdownInterval = null;
        }

        if (status === 'connected' || status === true) {
            statusEl.textContent = 'Connected';
            statusEl.className = 'status-item status-connected';
            statusEl.title = 'Real-time updates active';
        } else if (status === 'reconnecting') {
            const delaySeconds = Math.ceil(delay / 1000);
            let remainingSeconds = delaySeconds;

            // Update immediately
            statusEl.textContent = `Reconnecting (${remainingSeconds}s)...`;
            statusEl.className = 'status-item status-reconnecting';
            statusEl.title = `Attempt ${this.reconnectAttempts}, retrying in ${remainingSeconds}s`;

            // Start countdown
            this.statusCountdownInterval = setInterval(() => {
                remainingSeconds--;
                if (remainingSeconds > 0) {
                    statusEl.textContent = `Reconnecting (${remainingSeconds}s)...`;
                    statusEl.title = `Attempt ${this.reconnectAttempts}, retrying in ${remainingSeconds}s`;
                } else {
                    statusEl.textContent = 'Reconnecting...';
                    statusEl.title = `Attempting to reconnect (attempt ${this.reconnectAttempts})`;
                }
            }, 1000);
        } else {
            // 'disconnected' or false
            statusEl.textContent = 'Disconnected';
            statusEl.className = 'status-item status-disconnected';
            statusEl.title = 'Real-time updates unavailable';
        }
    }

    /**
     * Add item to activity feed
     * @param {string} type - Activity type (station, weather, message)
     * @param {string} text - Activity text
     */
    addActivityItem(type, text) {
        const timestamp = new Date().toLocaleTimeString();
        const item = { type, text, timestamp };

        // Add to front of array
        this.activityFeed.unshift(item);

        // Limit buffer size
        if (this.activityFeed.length > this.maxActivityItems) {
            this.activityFeed = this.activityFeed.slice(0, this.maxActivityItems);
        }

        // Render activity feed
        this.renderActivityFeed();
    }

    /**
     * Render activity feed to DOM
     */
    renderActivityFeed() {
        const container = document.getElementById('activity-list');
        if (!container) return;

        if (this.activityFeed.length === 0) {
            container.innerHTML = '<p class="empty-message">No activity yet</p>';
            return;
        }

        container.innerHTML = this.activityFeed.map(item => {
            // Make callsigns clickable in activity text
            const linkedText = this.linkifyCallsigns(item.text);
            return `<div class="activity-item ${item.type}">
                <span class="activity-timestamp">[${item.timestamp}]</span> ${linkedText}
            </div>`;
        }).join('');
    }

    /**
     * Convert callsigns in text to clickable links
     * @param {string} text - Text potentially containing callsigns
     * @returns {string} HTML with clickable callsign links
     */
    linkifyCallsigns(text) {
        // Pattern 1: "CALLSIGN1 → CALLSIGN2:" (message format - check first for priority)
        text = text.replace(/^([A-Z0-9\-\/]+)\s*→\s*([A-Z0-9\-\/]+):/g, (match, from, to) => {
            return `<span class="callsign-link" onclick="window.location.href='/station/${encodeURIComponent(from)}'">${from}</span> → <span class="callsign-link" onclick="window.location.href='/station/${encodeURIComponent(to)}'">${to}</span>:`;
        });

        // Pattern 2: "CALLSIGN (...) heard" or "CALLSIGN heard"
        text = text.replace(/^([A-Z0-9\-\/]+)(\s+\([^)]+\))?\s+(heard)/g, (match, callsign, gridPart, suffix) => {
            return `<span class="callsign-link" onclick="window.location.href='/station/${encodeURIComponent(callsign)}'">${callsign}</span>${gridPart || ''} ${suffix}`;
        });

        // Pattern 3: "CALLSIGN weather:" (not caught by pattern 2)
        text = text.replace(/^([A-Z0-9\-\/]+)(\s+weather:)/g, (match, callsign, suffix) => {
            return `<span class="callsign-link" onclick="window.location.href='/station/${encodeURIComponent(callsign)}'">${callsign}</span>${suffix}`;
        });

        return text;
    }

    /**
     * Clear activity feed
     */
    clearActivityFeed() {
        this.activityFeed = [];
        this.renderActivityFeed();
    }

    /**
     * Show monitored messages modal
     */
    async showMonitoredMessagesModal() {
        try {
            const data = await this.api.getMonitoredMessages(100);
            this.renderMonitoredMessages(data.messages);

            const modal = document.getElementById('monitored-messages-modal');
            if (modal) {
                modal.classList.add('show');
            }
        } catch (error) {
            console.error('Failed to load monitored messages:', error);
            this.showError('Failed to load monitored messages');
        }
    }

    /**
     * Render monitored messages to modal
     * @param {Array} messages - Array of message objects
     */
    renderMonitoredMessages(messages) {
        const container = document.getElementById('monitored-messages-list');
        if (!container) return;

        if (messages.length === 0) {
            container.innerHTML = '<p class="empty-message">No monitored messages yet</p>';
            return;
        }

        container.innerHTML = messages.map(msg => {
            const time = new Date(msg.timestamp).toLocaleString();
            return `<div class="monitored-message-item">
                <div class="monitored-message-header">
                    <div class="monitored-message-route">
                        <span class="monitored-from callsign-link" onclick="window.location.href='/station/${encodeURIComponent(msg.from_call)}'">${escapeHtml(msg.from_call)}</span>
                        <span class="monitored-arrow">→</span>
                        <span class="monitored-to callsign-link" onclick="window.location.href='/station/${encodeURIComponent(msg.to_call)}'">${escapeHtml(msg.to_call)}</span>
                    </div>
                    <div class="monitored-message-time">${time}</div>
                </div>
                <div class="monitored-message-text">${escapeHtml(msg.message)}</div>
            </div>`;
        }).join('');
    }

    /**
     * Hide modal
     */
    hideModal() {
        const modal = document.getElementById('monitored-messages-modal');
        if (modal) {
            modal.classList.remove('show');
        }
    }

    /**
     * Show error message to user
     * @param {string} message - Error message
     */
    showError(message) {
        console.error(message);
        // TODO: Implement user-visible error notifications
    }
}

// Initialize app on page load
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        const app = new APRSApp();
        app.init();
    });
} else {
    const app = new APRSApp();
    app.init();
}
