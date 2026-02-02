/**
 * APRS Map - Leaflet integration for station mapping
 */

export class APRSMap {
    constructor(elementId, options = {}) {
        // Options
        this.autoSave = options.autoSave !== undefined ? options.autoSave : true;
        this.useAPRSIcons = options.useAPRSIcons !== undefined ? options.useAPRSIcons : false;

        // Initialize Leaflet map
        this.map = L.map(elementId, {
            zoomControl: true,
            attributionControl: true
        }).setView([37.7749, -122.4194], 8);

        // Define base map layers
        const streetMap = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
            maxZoom: 19
        });

        const topoMap = L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
            attribution: 'Map data: © OpenStreetMap contributors, SRTM | Map style: © OpenTopoMap (CC-BY-SA)',
            maxZoom: 17
        });

        const terrainMap = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}', {
            attribution: 'Tiles © Esri &mdash; Source: Esri, DeLorme, NAVTEQ, TomTom, Intermap, iPC, USGS, FAO, NPS, NRCAN, GeoBase, Kadaster NL, Ordnance Survey, Esri Japan, METI, Esri China (Hong Kong), and the GIS User Community',
            maxZoom: 18
        });

        const satelliteMap = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
            attribution: 'Tiles © Esri &mdash; Source: Esri, i-cubed, USDA, USGS, AEX, GeoEye, Getmapping, Aerogrid, IGN, IGP, UPR-EGP, and the GIS User Community',
            maxZoom: 18
        });

        // Define base layers for layer control
        const baseLayers = {
            "Street": streetMap,
            "Terrain": terrainMap,
            "Topographic": topoMap,
            "Satellite": satelliteMap
        };

        // Add default layer (Terrain)
        terrainMap.addTo(this.map);

        // Add layer control to switch between map types
        L.control.layers(baseLayers, null, {
            position: 'topleft',
            collapsed: false
        }).addTo(this.map);

        this.markers = {}; // callsign -> marker object
        this.stationData = {}; // callsign -> station data (for icon refresh)
        this.digipeaterCoverageLayer = null; // Layer group for digipeater coverage
        this.showDigipeaterCoverage = false; // Toggle for coverage display
        this.clickedDigipeaterCoverage = null; // Layer for individual clicked digipeater
        this.clickedDigipeaterCallsign = null; // Track which digipeater is currently shown
        this.hiddenStations = new Set(); // Track stations hidden during digipeater focus
        this.stationPathLayer = null; // Layer for individual station path history (click mode)
        this.stationPathCallsign = null; // Track which station path is currently shown
        this.allPathsLayer = null; // Layer for all paths (filter mode)
        this.showingAllPaths = false; // Track if showing all paths via filter
        this.clickedLocalCoverage = false; // Track if local coverage is shown via click
        this.mycall = null; // Store MYCALL for quick lookups
        this.pathCache = new Map(); // callsign -> {positions, fetchedAt} cache
        this.CACHE_TTL = 60000; // 1 minute cache TTL
        this.pathLoadingIndicator = null; // Loading progress indicator

        // Local station (GPS position)
        this.localStationMarker = null; // Marker for our GPS position
        this.localStationPosition = null; // Current GPS position {lat, lon}
        this.localCoverageLayer = null; // Coverage circle for local station
        this.showLocalCoverage = false; // Toggle for local coverage display

        // Restore saved map position if auto-save is enabled (main console map only)
        if (this.autoSave) {
            this.restoreMapState();

            // Save map state whenever user moves or zooms the map
            this.map.on('moveend', () => this.saveMapState());
            this.map.on('zoomend', () => this.saveMapState());
        }

        // Clear clicked elements when clicking on map background
        this.map.on('click', (e) => {
            // Only clear if not clicking on a marker (marker clicks are handled separately)
            if (!e.originalEvent.target.closest('.leaflet-marker-icon')) {
                this.clearClickedDigipeaterCoverage();
                this.clearStationPath();
                this.clearClickedLocalCoverage();
            }
        });

        // Fetch MYCALL on initialization
        this.fetchMycall();
    }

    /**
     * Fetch MYCALL from API
     */
    async fetchMycall() {
        try {
            const response = await fetch('/api/status');
            if (response.ok) {
                const status = await response.json();
                this.mycall = status.mycall;
                console.log(`MYCALL set to: ${this.mycall}`);
            }
        } catch (error) {
            console.error('Failed to fetch MYCALL:', error);
        }
    }

    /**
     * Validate that position is not at "Null Island" (0.0, 0.0)
     * @param {number} lat - Latitude
     * @param {number} lon - Longitude
     * @returns {boolean} - True if valid, false if Null Island
     */
    isValidPosition(lat, lon) {
        // Reject exactly 0.0, 0.0 (Null Island - invalid GPS data)
        if (lat === 0.0 && lon === 0.0) {
            return false;
        }
        // Also reject null/undefined/NaN
        if (lat == null || lon == null || isNaN(lat) || isNaN(lon)) {
            return false;
        }
        return true;
    }

    /**
     * Calculate convex hull of a set of points using Gift wrapping (Jarvis march) algorithm
     * More robust for geographic coordinates than Graham scan
     * @param {Array} points - Array of [lat, lon] points
     * @returns {Array} - Array of [lat, lon] points forming convex hull
     */
    convexHull(points) {
        if (points.length < 3) {
            return points;
        }

        // Remove duplicate points
        const uniquePoints = [];
        const seen = new Set();
        for (const p of points) {
            const key = `${p[0]},${p[1]}`;
            if (!seen.has(key)) {
                seen.add(key);
                uniquePoints.push(p);
            }
        }

        if (uniquePoints.length < 3) {
            return uniquePoints;
        }

        // Find the leftmost point (westernmost, then southernmost if tied)
        let leftmost = uniquePoints[0];
        for (let i = 1; i < uniquePoints.length; i++) {
            if (uniquePoints[i][1] < leftmost[1] ||
                (uniquePoints[i][1] === leftmost[1] && uniquePoints[i][0] < leftmost[0])) {
                leftmost = uniquePoints[i];
            }
        }

        const hull = [];
        let currentPoint = leftmost;
        let nextPoint;

        do {
            hull.push(currentPoint);
            nextPoint = uniquePoints[0];

            // Find the most counter-clockwise point from currentPoint
            for (let i = 1; i < uniquePoints.length; i++) {
                if (nextPoint === currentPoint ||
                    this.crossProduct(currentPoint, nextPoint, uniquePoints[i]) > 0) {
                    nextPoint = uniquePoints[i];
                }
            }

            currentPoint = nextPoint;
        } while (currentPoint !== leftmost && hull.length < uniquePoints.length + 1);

        return hull;
    }

    /**
     * Calculate cross product of vectors (p1->p2) and (p1->p3)
     * @param {Array} p1 - Point [lat, lon]
     * @param {Array} p2 - Point [lat, lon]
     * @param {Array} p3 - Point [lat, lon]
     * @returns {number} - Cross product (positive = counter-clockwise)
     */
    crossProduct(p1, p2, p3) {
        return (p2[0] - p1[0]) * (p3[1] - p1[1]) - (p2[1] - p1[1]) * (p3[0] - p1[0]);
    }

    /**
     * Add or update a station marker on the map
     * @param {Object} station - Station data object
     */
    addOrUpdateStation(station) {
        if (!station.has_position || !station.last_position) {
            return;
        }

        const pos = station.last_position;
        
        // Filter out invalid "Null Island" coordinates (0.0, 0.0)
        if (!this.isValidPosition(pos.latitude, pos.longitude)) {
            console.debug(`Ignoring invalid position (Null Island) for ${station.callsign}`);
            return;
        }
        
        const latLng = [pos.latitude, pos.longitude];

        // Store station data for icon refresh
        this.stationData[station.callsign] = station;

        // Create or update marker
        if (this.markers[station.callsign]) {
            const marker = this.markers[station.callsign];
            marker.setLatLng(latLng);
            // Update popup in case station data changed
            const popupContent = this.createPopupContent(station);
            marker.setPopupContent(popupContent);

            // Apply ping animation on update
            const markerElement = marker.getElement();
            if (markerElement) {
                // Choose animation based on icon type
                // Use glow for APRS symbol icons, ping for simple markers
                const animationClass = this.useAPRSIcons ? 'marker-glow' : 'marker-ping';

                // Remove any existing animation classes first
                markerElement.classList.remove('marker-ping', 'marker-glow');

                // Force reflow to restart animation
                void markerElement.offsetWidth;

                // Add animation class
                markerElement.classList.add(animationClass);

                // Remove animation class after it completes
                const animationDuration = this.useAPRSIcons ? 1200 : 1500; // ms
                setTimeout(() => {
                    markerElement.classList.remove(animationClass);
                }, animationDuration);
            }
        } else {
            const marker = L.marker(latLng, {
                icon: this.createAPRSIcon(pos.symbol_table, pos.symbol_code, station, this.useAPRSIcons),
                title: station.callsign
            });

            // Create popup content
            const popupContent = this.createPopupContent(station);
            marker.bindPopup(popupContent);

            // Add click event to show digipeater coverage and/or station path
            marker.on('click', async (e) => {
                // Check for nearby overlapping stations
                const nearbyStations = this.findNearbyStations(station, 0.001); // ~111m at equator

                if (nearbyStations.length > 1) {
                    // Multiple stations at this location - show list
                    const popupContent = this.createMultiStationPopup(nearbyStations);
                    marker.setPopupContent(popupContent);
                    marker.openPopup();
                    return;
                }

                // Single station - restore normal popup and proceed with normal click behavior
                marker.setPopupContent(this.createPopupContent(station));

                // Fetch MYCALL if not cached
                if (!this.mycall) {
                    try {
                        const response = await fetch('/api/status');
                        if (response.ok) {
                            const status = await response.json();
                            this.mycall = status.mycall;
                        }
                    } catch (error) {
                        console.error('Failed to fetch MYCALL:', error);
                    }
                }

                // Check if this is the local station (MYCALL)
                const isLocalStation = this.mycall && station.callsign === this.mycall;

                if (isLocalStation) {
                    // Show local coverage (zero-hop) when clicking own station
                    await this.showClickedLocalCoverage();
                } else {
                    // Show position history path for other stations
                    await this.showStationPath(station.callsign);

                    // Clear local coverage if it was clicked
                    this.clearClickedLocalCoverage();

                    // Also show digipeater coverage if applicable
                    if (station.is_digipeater) {
                        this.showClickedDigipeaterCoverage(station.callsign);
                    } else {
                        // Clear clicked coverage when clicking non-digipeater
                        this.clearClickedDigipeaterCoverage();
                    }
                }
            });

            marker.addTo(this.map);
            this.markers[station.callsign] = marker;
        }
    }

    /**
     * Create popup content for a station
     * @param {Object} station - Station data
     * @returns {string} HTML content for popup
     */
    createPopupContent(station) {
        const pos = station.last_position;
        let content = `<div class="marker-popup">`;
        content += `<strong><span class="callsign-link" onclick="window.location.href='/station/${encodeURIComponent(station.callsign)}'">${station.callsign}</span></strong><br>`;

        if (pos.grid_square) {
            content += `Grid: ${pos.grid_square}<br>`;
        }

        if (pos.comment) {
            content += `${pos.comment}<br>`;
        }

        if (station.has_weather && station.last_weather) {
            const wx = station.last_weather;
            content += `<br><strong>Weather:</strong><br>`;
            if (wx.temperature !== null) {
                content += `Temp: ${wx.temperature}°F<br>`;
            }
            if (wx.humidity !== null) {
                content += `Humidity: ${wx.humidity}%<br>`;
            }
            if (wx.wind_speed !== null) {
                content += `Wind: ${wx.wind_speed} mph`;
                if (wx.wind_direction !== null) {
                    content += ` @ ${wx.wind_direction}°`;
                }
                content += `<br>`;
            }
        }

        content += `<br><a href="/station/${encodeURIComponent(station.callsign)}">View Details →</a>`;
        content += `</div>`;

        return content;
    }

    /**
     * Find stations near a given station (for handling overlapping markers)
     * @param {Object} station - Reference station
     * @param {number} threshold - Distance threshold in degrees (~0.001 = 111m at equator)
     * @returns {Array} Array of nearby stations including the reference station
     */
    findNearbyStations(station, threshold = 0.001) {
        if (!station.last_position) return [station];

        const refLat = station.last_position.latitude;
        const refLon = station.last_position.longitude;
        const nearby = [];

        // Check all stations for proximity
        for (const [callsign, stationData] of Object.entries(this.stationData)) {
            if (!stationData.last_position) continue;

            const lat = stationData.last_position.latitude;
            const lon = stationData.last_position.longitude;

            // Simple rectangular distance check (fast approximation)
            const latDiff = Math.abs(lat - refLat);
            const lonDiff = Math.abs(lon - refLon);

            if (latDiff <= threshold && lonDiff <= threshold) {
                nearby.push(stationData);
            }
        }

        return nearby;
    }

    /**
     * Create popup content for multiple overlapping stations
     * @param {Array} stations - Array of station objects
     * @returns {string} HTML content for popup
     */
    createMultiStationPopup(stations) {
        let content = `<div class="marker-popup multi-station-popup">`;
        content += `<strong>${stations.length} Stations at this location</strong><br>`;
        content += `<small>Click a callsign to view details</small><br><br>`;

        // Sort stations by callsign
        const sortedStations = [...stations].sort((a, b) =>
            a.callsign.localeCompare(b.callsign)
        );

        // List each station
        sortedStations.forEach(station => {
            const pos = station.last_position;
            content += `<div style="margin: 5px 0; padding: 5px 0; border-bottom: 1px solid #ddd;">`;
            content += `<strong><span class="callsign-link" onclick="window.location.href='/station/${encodeURIComponent(station.callsign)}'">${station.callsign}</span></strong>`;

            // Add device info if available
            if (station.device) {
                content += `<br><small style="color: #666;">${station.device}</small>`;
            }

            // Add grid square
            if (pos.grid_square) {
                content += `<br><small>Grid: ${pos.grid_square}</small>`;
            }

            // Add comment if available
            if (pos.comment && pos.comment.trim()) {
                const shortComment = pos.comment.length > 40
                    ? pos.comment.substring(0, 40) + '...'
                    : pos.comment;
                content += `<br><small>${shortComment}</small>`;
            }

            content += `</div>`;
        });

        content += `</div>`;
        return content;
    }

    /**
     * Create APRS icon for station
     * @param {string} symbolTable - APRS symbol table character
     * @param {string} symbolCode - APRS symbol code character
     * @param {Object} station - Station object with has_weather and is_digipeater properties
     * @param {boolean} useAPRSIcons - Whether to use APRS sprite icons or simple markers
     * @returns {L.Icon} Leaflet icon
     */
    createAPRSIcon(symbolTable, symbolCode, station, useAPRSIcons = false) {
        // Use simple colored markers if APRS icons are disabled
        if (!useAPRSIcons) {
            // Determine marker color based on station type
            let color, cssClass;
            if (station.has_weather && station.is_digipeater) {
                color = '#ff8c00';  // Orange for weather + digipeater
                cssClass = 'weather-digipeater';
            } else if (station.has_weather) {
                color = '#ffd700';  // Yellow for weather stations
                cssClass = 'weather';
            } else if (station.is_digipeater) {
                color = '#3388ff';  // Blue for digipeaters
                cssClass = 'digipeater';
            } else {
                color = '#4CAF50';  // Green for end stations
                cssClass = 'station';
            }

            return L.divIcon({
                className: 'aprs-marker',
                html: `<div class="marker-dot ${cssClass}"></div>`,
                iconSize: [12, 12],
                iconAnchor: [6, 6],
                popupAnchor: [0, -6]
            });
        }

        // Calculate sprite position from APRS symbol table and code
        // APRS symbols are in a 16x6 grid (96 symbols per table)
        // Primary table (/) uses top 6 rows, alternate table (\) uses bottom 6 rows
        const isPrimary = symbolTable === '/';
        const codeNum = symbolCode.charCodeAt(0) - 33; // ASCII offset from '!'

        const col = codeNum % 16;
        const row = Math.floor(codeNum / 16) + (isPrimary ? 0 : 6);

        // Each symbol is 24x24 pixels in the sprite sheet
        const x = col * 24;
        const y = row * 24;

        console.log(`APRS Icon: table=${symbolTable} code=${symbolCode} (${symbolCode.charCodeAt(0)}) -> col=${col} row=${row} pos=(${x},${y})`);

        return L.divIcon({
            className: 'aprs-symbol-icon',
            html: `<div style="width:24px;height:24px;background-image:url(/static/vendor/aprs-symbols.png);background-position:-${x}px -${y}px;background-repeat:no-repeat;display:block;"></div>`,
            iconSize: [24, 24],
            iconAnchor: [12, 12],
            popupAnchor: [0, -12]
        });
    }

    /**
     * Remove a station marker from the map
     * @param {string} callsign - Station callsign
     */
    removeStation(callsign) {
        if (this.markers[callsign]) {
            this.map.removeLayer(this.markers[callsign]);
            delete this.markers[callsign];
            delete this.stationData[callsign];
            // Also remove from hidden stations set if present
            this.hiddenStations.delete(callsign);
        }
    }

    /**
     * Toggle between APRS icons and simple markers
     * @param {boolean} useAPRSIcons - Whether to use APRS sprite icons
     */
    setUseAPRSIcons(useAPRSIcons) {
        this.useAPRSIcons = useAPRSIcons;
        this.refreshAllMarkers();
    }

    /**
     * Refresh all markers (used when toggling icon types)
     */
    refreshAllMarkers() {
        // Remove all current markers
        Object.values(this.markers).forEach(marker => {
            this.map.removeLayer(marker);
        });
        this.markers = {};

        // Re-add all stations with new icon style
        Object.values(this.stationData).forEach(station => {
            this.addOrUpdateStation(station);
        });
    }

    /**
     * Fit map bounds to show all stations
     */
    fitBounds() {
        const positions = Object.values(this.markers).map(m => m.getLatLng());
        if (positions.length > 0) {
            const bounds = L.latLngBounds(positions);
            this.map.fitBounds(bounds, {
                padding: [50, 50],
                maxZoom: 15
            });
        }
    }

    /**
     * Center map on a specific station
     * @param {string} callsign - Station callsign
     * @param {number} zoom - Zoom level (optional)
     */
    centerOnStation(callsign, zoom = 13) {
        const marker = this.markers[callsign];
        if (marker) {
            this.map.setView(marker.getLatLng(), zoom);
            marker.openPopup();
        }
    }

    /**
     * Get marker count
     * @returns {number} Number of markers on map
     */
    getMarkerCount() {
        return Object.keys(this.markers).length;
    }

    /**
     * Save current map state to localStorage
     */
    saveMapState() {
        if (!this.autoSave) return; // Only save for main console map

        try {
            const center = this.map.getCenter();
            const zoom = this.map.getZoom();
            const state = {
                lat: center.lat,
                lng: center.lng,
                zoom: zoom
            };
            localStorage.setItem('aprs_map_state', JSON.stringify(state));
        } catch (e) {
            console.warn('Failed to save map state:', e);
        }
    }

    /**
     * Restore map state from localStorage
     */
    restoreMapState() {
        try {
            const savedState = localStorage.getItem('aprs_map_state');
            if (savedState) {
                const state = JSON.parse(savedState);
                this.map.setView([state.lat, state.lng], state.zoom);
            }
        } catch (e) {
            console.warn('Failed to restore map state:', e);
        }
    }

    /**
     * Toggle digipeater coverage visualization
     * @param {boolean} show - Whether to show coverage
     * @param {string} timeRange - Time range filter ('all', '1h', '4h', '24h')
     */
    async toggleDigipeaterCoverage(show, timeRange = 'all') {
        this.showDigipeaterCoverage = show;

        // Clear all clicked visualizations when toggling global coverage
        this.clearClickedDigipeaterCoverage();
        this.clearStationPath();
        this.clearClickedLocalCoverage();

        if (show) {
            await this.updateDigipeaterCoverage(timeRange);
        } else {
            this.clearDigipeaterCoverage();
        }
    }

    /**
     * Update digipeater coverage layer
     * @param {string} timeRange - Time range filter ('all', '1h', '4h', '24h')
     */
    async updateDigipeaterCoverage(timeRange = 'all') {
        try {
            const response = await fetch('/api/digipeaters');
            const data = await response.json();

            // Calculate cutoff time based on filter
            let cutoffTime = null;
            if (timeRange !== 'all') {
                const now = new Date();
                switch (timeRange) {
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
            }

            // Clear existing coverage
            this.clearDigipeaterCoverage();

            // Create new layer group
            this.digipeaterCoverageLayer = L.layerGroup().addTo(this.map);

            // Add coverage circles for each digipeater
            Object.values(data.digipeaters).forEach(digi => {
                this.addDigipeaterCoverageCircle(digi, cutoffTime);
            });

            console.log(`Loaded coverage for ${data.count} digipeaters (time range: ${timeRange})`);
        } catch (error) {
            console.error('Failed to load digipeater coverage:', error);
        }
    }

    /**
     * Add coverage polygon for a single digipeater
     * @param {Object} digi - Digipeater data
     * @param {Date} cutoffTime - Optional time filter (only include stations heard after this time)
     */
    addDigipeaterCoverageCircle(digi, cutoffTime = null) {
        if (!digi.has_position || !digi.position) {
            // Can't show coverage without digipeater position
            return;
        }

        const digiLatLng = [digi.position.latitude, digi.position.longitude];

        // Filter stations by time range if specified
        let stationsToInclude = digi.stations_heard.filter(s => s.position);
        if (cutoffTime) {
            stationsToInclude = stationsToInclude.filter(s => {
                if (!s.last_heard) return false;
                const stationTime = new Date(s.last_heard);
                return stationTime >= cutoffTime;
            });
        }

        // Collect all valid station positions
        const stationPoints = [];
        let maxDistance = 0;

        stationsToInclude.forEach(station => {
            // Skip Null Island positions
            if (!this.isValidPosition(station.position.latitude, station.position.longitude)) {
                return;
            }

            const stationLatLng = [station.position.latitude, station.position.longitude];
            stationPoints.push(stationLatLng);

            // Track max distance for popup display
            const distance = L.latLng(digiLatLng).distanceTo(stationLatLng);
            if (distance > maxDistance) {
                maxDistance = distance;
            }
        });

        // Don't show coverage if no stations with positions
        if (stationPoints.length === 0) {
            return;
        }

        // Add digipeater position to the point set for hull calculation
        const allPoints = [digiLatLng, ...stationPoints];

        // Calculate convex hull for coverage polygon
        const hullPoints = this.convexHull(allPoints);

        console.log(`Digipeater ${digi.callsign}: ${allPoints.length} points → ${hullPoints.length} hull points`);
        console.log('Hull points:', hullPoints);

        // Create coverage polygon with light blue fill
        const polygon = L.polygon(hullPoints, {
            color: '#3388ff',
            fillColor: '#3388ff',
            fillOpacity: 0.08,
            weight: 2,
            dashArray: '5, 5'
        });

        // Add popup with digipeater info (show filtered count)
        const popupContent = `
            <div class="digipeater-popup">
                <strong><span class="callsign-link" onclick="window.location.href='/station/${encodeURIComponent(digi.callsign)}'">${digi.callsign}</span></strong> (Digipeater)<br>
                <small>Max range: ${(maxDistance / 1000).toFixed(1)} km</small><br>
                <small>Stations heard: ${stationsToInclude.length}</small><br>
                <small>Grid: ${digi.position.grid_square || 'N/A'}</small>
            </div>
        `;
        polygon.bindPopup(popupContent);

        // Add digipeater marker
        const marker = L.circleMarker(digiLatLng, {
            radius: 8,
            fillColor: '#ff7800',
            color: '#fff',
            weight: 2,
            opacity: 1,
            fillOpacity: 0.8
        });
        marker.bindPopup(popupContent);

        // Add to layer group
        polygon.addTo(this.digipeaterCoverageLayer);
        marker.addTo(this.digipeaterCoverageLayer);
    }

    /**
     * Clear digipeater coverage layer
     */
    clearDigipeaterCoverage() {
        if (this.digipeaterCoverageLayer) {
            this.digipeaterCoverageLayer.clearLayers();
            this.map.removeLayer(this.digipeaterCoverageLayer);
            this.digipeaterCoverageLayer = null;
        }
    }

    /**
     * Show coverage polygon for a clicked digipeater
     * @param {string} callsign - Digipeater callsign
     */
    async showClickedDigipeaterCoverage(callsign) {
        // If clicking the same digipeater, toggle it off
        if (this.clickedDigipeaterCallsign === callsign) {
            this.clearClickedDigipeaterCoverage();
            return;
        }

        // Clear any existing clicked coverage
        this.clearClickedDigipeaterCoverage();

        try {
            const response = await fetch(`/api/digipeaters/${encodeURIComponent(callsign)}`);
            if (!response.ok) {
                console.log(`Digipeater ${callsign} has no coverage data`);
                return;
            }

            const digiData = await response.json();

            if (!digiData.has_position || !digiData.position) {
                console.log(`Digipeater ${callsign} has no position`);
                return;
            }

            // Create layer group for this digipeater's coverage
            this.clickedDigipeaterCoverage = L.layerGroup().addTo(this.map);
            this.clickedDigipeaterCallsign = callsign;

            // Draw the coverage polygon (reuse the existing method logic)
            const digiLatLng = [digiData.position.latitude, digiData.position.longitude];
            const stationPoints = [];
            let maxDistance = 0;

            const stationsWithPos = digiData.stations_heard.filter(s =>
                s.position &&
                this.isValidPosition(s.position.latitude, s.position.longitude)
            );

            stationsWithPos.forEach(station => {
                const stationLatLng = [station.position.latitude, station.position.longitude];
                stationPoints.push(stationLatLng);

                const distance = L.latLng(digiLatLng).distanceTo(stationLatLng);
                if (distance > maxDistance) {
                    maxDistance = distance;
                }
            });

            if (maxDistance === 0) {
                console.log(`Digipeater ${callsign} has no stations with positions`);
                this.clearClickedDigipeaterCoverage();
                return;
            }

            // Calculate convex hull
            const allPoints = [digiLatLng, ...stationPoints];
            const hullPoints = this.convexHull(allPoints);

            // Create coverage polygon with slightly more visible fill
            const polygon = L.polygon(hullPoints, {
                color: '#3388ff',
                fillColor: '#3388ff',
                fillOpacity: 0.15,  // Slightly more opaque for clicked coverage
                weight: 3,  // Thicker border
                dashArray: '5, 5'
            });

            const popupContent = `
                <div class="digipeater-popup">
                    <strong>${callsign} Coverage</strong><br>
                    <small>Max range: ${(maxDistance / 1000).toFixed(1)} km</small><br>
                    <small>Stations heard: ${stationsWithPos.length}</small><br>
                    <small style="color: #ffa500;">📡 Showing only heard stations</small><br>
                    <small>Click again to show all</small>
                </div>
            `;
            polygon.bindPopup(popupContent);
            polygon.addTo(this.clickedDigipeaterCoverage);

            // Filter stations to only show those heard by this digipeater
            this.filterStationsForDigipeater(callsign, digiData.stations_heard);

            console.log(`Showing coverage for digipeater ${callsign} with ${stationsWithPos.length} stations`);
        } catch (error) {
            console.error(`Failed to load coverage for ${callsign}:`, error);
            this.clearClickedDigipeaterCoverage();
        }
    }

    /**
     * Filter visible stations to only show those heard by a specific digipeater
     * @param {string} digiCallsign - Digipeater callsign
     * @param {Array} heardStations - Array of station objects heard by this digipeater
     */
    filterStationsForDigipeater(digiCallsign, heardStations) {
        // Create set of callsigns heard by this digipeater
        const heardCallsigns = new Set(heardStations.map(s => s.callsign));
        heardCallsigns.add(digiCallsign); // Always show the digipeater itself

        // Hide all stations not heard by this digipeater
        this.hiddenStations.clear();
        Object.keys(this.markers).forEach(callsign => {
            if (!heardCallsigns.has(callsign)) {
                // Hide this station
                const marker = this.markers[callsign];
                if (marker) {
                    this.map.removeLayer(marker);
                    this.hiddenStations.add(callsign);
                }
            }
        });

        console.log(`Filtered map to ${heardCallsigns.size} stations (hid ${this.hiddenStations.size})`);
    }

    /**
     * Restore all hidden stations after digipeater focus is cleared
     */
    restoreHiddenStations() {
        this.hiddenStations.forEach(callsign => {
            const marker = this.markers[callsign];
            if (marker) {
                marker.addTo(this.map);
            }
        });
        console.log(`Restored ${this.hiddenStations.size} hidden stations`);
        this.hiddenStations.clear();
    }

    /**
     * Clear clicked digipeater coverage layer
     */
    clearClickedDigipeaterCoverage() {
        if (this.clickedDigipeaterCoverage) {
            this.clickedDigipeaterCoverage.clearLayers();
            this.map.removeLayer(this.clickedDigipeaterCoverage);
            this.clickedDigipeaterCoverage = null;
            this.clickedDigipeaterCallsign = null;
        }

        // Restore any hidden stations
        this.restoreHiddenStations();
    }

    /**
     * Show position history path for a clicked station
     * @param {string} callsign - Station callsign
     */
    async showStationPath(callsign) {
        // If clicking the same station, toggle it off
        if (this.stationPathCallsign === callsign) {
            this.clearStationPath();
            return;
        }

        // Clear any existing path
        this.clearStationPath();

        try {
            // Fetch full station details including position history
            const response = await fetch(`/api/stations/${encodeURIComponent(callsign)}`);
            if (!response.ok) {
                console.log(`Station ${callsign} not found`);
                return;
            }

            const station = await response.json();

            // Check if station has position history
            if (!station.position_history || station.position_history.length < 2) {
                console.log(`Station ${callsign} has no position history`);
                return;
            }

            // Filter out Null Island positions
            const validPositions = station.position_history.filter(p =>
                !(p.latitude === 0.0 && p.longitude === 0.0)
            );

            if (validPositions.length < 2) {
                console.log(`Station ${callsign} has insufficient valid positions`);
                return;
            }

            // Create layer group for this station's path
            this.stationPathLayer = L.layerGroup().addTo(this.map);
            this.stationPathCallsign = callsign;

            // Sort by timestamp (oldest first for drawing)
            const sortedPositions = validPositions.sort((a, b) =>
                new Date(a.timestamp) - new Date(b.timestamp)
            );

            // Extract lat/lng points for polyline
            const pathPoints = sortedPositions.map(p => [p.latitude, p.longitude]);

            // Draw path as polyline
            const pathLine = L.polyline(pathPoints, {
                color: '#00aaff',
                weight: 3,
                opacity: 0.7,
                smoothFactor: 1
            });

            // Add popup to path showing summary
            const firstTime = new Date(sortedPositions[0].timestamp);
            const lastTime = new Date(sortedPositions[sortedPositions.length - 1].timestamp);
            const timeSpan = ((lastTime - firstTime) / (1000 * 60 * 60)).toFixed(1); // hours

            pathLine.bindPopup(`
                <div class="path-popup">
                    <strong>${callsign} Movement Path</strong><br>
                    <small>Positions: ${validPositions.length}</small><br>
                    <small>Time span: ${timeSpan} hours</small><br>
                    <small>Oldest: ${firstTime.toLocaleString()}</small><br>
                    <small>Newest: ${lastTime.toLocaleString()}</small><br>
                    <small style="color: #ffa500;">Click station again to hide</small>
                </div>
            `);
            pathLine.addTo(this.stationPathLayer);

            // Add markers for historical positions (except the most recent)
            sortedPositions.forEach((pos, index) => {
                // Skip the most recent position (already shown as main marker)
                if (index === sortedPositions.length - 1) return;

                // Calculate opacity based on age (older = more transparent)
                const opacity = 0.3 + (index / sortedPositions.length) * 0.5;

                // Create small marker for historical position
                const historicalMarker = L.circleMarker([pos.latitude, pos.longitude], {
                    radius: 4,
                    fillColor: '#00aaff',
                    color: '#fff',
                    weight: 1,
                    opacity: opacity + 0.2,
                    fillOpacity: opacity
                });

                // Add popup with timestamp and position info
                const timeStr = new Date(pos.timestamp).toLocaleString();
                const age = ((lastTime - new Date(pos.timestamp)) / (1000 * 60)).toFixed(0); // minutes ago

                historicalMarker.bindPopup(`
                    <div class="path-popup">
                        <strong>${callsign}</strong><br>
                        <small>${timeStr}</small><br>
                        <small>${age} minutes ago</small><br>
                        <small>${pos.latitude.toFixed(6)}, ${pos.longitude.toFixed(6)}</small>
                        ${pos.grid_square ? `<br><small>Grid: ${pos.grid_square}</small>` : ''}
                    </div>
                `);

                historicalMarker.addTo(this.stationPathLayer);
            });

            console.log(`Showing path for ${callsign} with ${validPositions.length} positions`);
        } catch (error) {
            console.error(`Failed to load path for ${callsign}:`, error);
            this.clearStationPath();
        }
    }

    /**
     * Clear station path layer
     */
    clearStationPath() {
        if (this.stationPathLayer) {
            this.stationPathLayer.clearLayers();
            this.map.removeLayer(this.stationPathLayer);
            this.stationPathLayer = null;
            this.stationPathCallsign = null;
        }
    }

    /**
     * Show paths for all stations with has_path=true
     * @param {Array} stations - Array of station objects with has_path flag
     */
    /**
     * Show paths for all stations (optimized with batch API, time filtering, and progressive rendering)
     * @param {Array} stations - Array of station objects
     * @param {string} timeFilter - Time filter value ('all', '1h', '4h', '24h')
     */
    async showAllPaths(stations, timeFilter = 'all') {
        // Clear existing all-paths layer
        this.clearAllPaths();

        // Filter to stations with paths
        const stationsWithPaths = stations.filter(s => s.has_path === true);

        if (stationsWithPaths.length === 0) {
            console.log('No stations with paths to display');
            return;
        }

        console.log(`[Path Rendering] Showing paths for ${stationsWithPaths.length} stations with time filter: ${timeFilter}`);

        // Create layer group for all paths
        this.allPathsLayer = L.layerGroup().addTo(this.map);
        this.showingAllPaths = true;

        // Calculate cutoff time from filter (Optimization #2: Time-Based Filtering)
        const cutoffTime = this.getTimeFilterCutoff(timeFilter);
        const cutoffTimestamp = cutoffTime ? Math.floor(cutoffTime.getTime() / 1000) : null;

        // Show progress indicator (Optimization #4: Progressive Rendering)
        this.showPathLoadingIndicator(stationsWithPaths.length);

        const startTime = performance.now();

        // Optimization #1: Batch API - Fetch all paths in one request
        try {
            const response = await fetch('/api/stations/paths', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    callsigns: stationsWithPaths.map(s => s.callsign),
                    cutoff_time: cutoffTimestamp
                })
            });

            if (!response.ok) {
                console.error('Failed to fetch station paths:', response.statusText);
                this.hidePathLoadingIndicator();
                return;
            }

            const pathsData = await response.json();
            const fetchTime = performance.now() - startTime;
            console.log(`[Path Rendering] Batch fetch completed in ${fetchTime.toFixed(0)}ms`);

            // Cache the fetched data (Optimization #5: Caching - implicit via batch)
            const now = Date.now();
            for (const [callsign, positions] of Object.entries(pathsData)) {
                this.pathCache.set(callsign, {
                    positions: positions,
                    fetchedAt: now
                });
            }

            // Optimization #4: Progressive Rendering - Draw paths in batches
            const BATCH_SIZE = 10; // Process 10 stations at a time
            const callsigns = Object.keys(pathsData);
            let drawnCount = 0;

            for (let i = 0; i < callsigns.length; i += BATCH_SIZE) {
                const batch = callsigns.slice(i, i + BATCH_SIZE);

                // Draw paths for this batch
                for (const callsign of batch) {
                    const positions = pathsData[callsign];
                    if (this.drawStationPath(callsign, positions, timeFilter)) {
                        drawnCount++;
                    }
                }

                this.updatePathLoadingIndicator(i + batch.length);

                // Yield to browser to keep UI responsive
                await new Promise(resolve => setTimeout(resolve, 0));
            }

            const totalTime = performance.now() - startTime;
            console.log(`[Path Rendering] Drew ${drawnCount} paths in ${totalTime.toFixed(0)}ms (${(totalTime / drawnCount).toFixed(1)}ms per path)`);

        } catch (error) {
            console.error('Failed to load station paths:', error);
        } finally {
            this.hidePathLoadingIndicator();
        }
    }

    /**
     * Draw a single station's path on the map
     * @param {string} callsign - Station callsign
     * @param {Array} positions - Array of position objects
     * @param {string} timeFilter - Current time filter
     * @returns {boolean} True if path was drawn, false otherwise
     */
    drawStationPath(callsign, positions, timeFilter) {
        if (!positions || positions.length < 2) return false;

        // Filter out Null Island positions
        let validPositions = positions.filter(p =>
            !(p.latitude === 0.0 && p.longitude === 0.0)
        );

        // Apply time filter (Optimization #2: Time-Based Position Filtering)
        const cutoffTime = this.getTimeFilterCutoff(timeFilter);
        if (cutoffTime) {
            validPositions = validPositions.filter(p =>
                new Date(p.timestamp) >= cutoffTime
            );
        }

        if (validPositions.length < 2) return false;

        // Sort by timestamp (oldest first for drawing)
        const sortedPositions = validPositions.sort((a, b) =>
            new Date(a.timestamp) - new Date(b.timestamp)
        );

        // Extract lat/lng points for polyline
        const pathPoints = sortedPositions.map(p => [p.latitude, p.longitude]);

        // Draw path as polyline (Optimization #3: Path Simplification)
        const pathLine = L.polyline(pathPoints, {
            color: '#00aaff',
            weight: 2,
            opacity: 0.5,
            smoothFactor: 3.0  // Increased from 1.0 for better performance
        });

        // Add popup to path showing summary
        const firstTime = new Date(sortedPositions[0].timestamp);
        const lastTime = new Date(sortedPositions[sortedPositions.length - 1].timestamp);
        const timeSpan = ((lastTime - firstTime) / (1000 * 60 * 60)).toFixed(1); // hours

        pathLine.bindPopup(`
            <div class="path-popup">
                <strong>${callsign} Movement Path</strong><br>
                <small>Positions: ${validPositions.length}</small><br>
                <small>Time span: ${timeSpan} hours</small>
            </div>
        `);
        pathLine.addTo(this.allPathsLayer);

        return true;
    }

    /**
     * Get cutoff time based on time filter
     * @param {string} timeFilter - Filter value ('all', '1h', '4h', '24h')
     * @returns {Date|null} Cutoff time or null for 'all'
     */
    getTimeFilterCutoff(timeFilter) {
        if (!timeFilter || timeFilter === 'all') return null;

        const now = new Date();
        switch (timeFilter) {
            case '1h':
                return new Date(now - 60 * 60 * 1000);
            case '4h':
                return new Date(now - 4 * 60 * 60 * 1000);
            case '24h':
                return new Date(now - 24 * 60 * 60 * 1000);
            default:
                return null;
        }
    }

    /**
     * Show path loading progress indicator
     * @param {number} total - Total number of paths to load
     */
    showPathLoadingIndicator(total) {
        // Create floating progress indicator
        if (!this.pathLoadingIndicator) {
            this.pathLoadingIndicator = document.createElement('div');
            this.pathLoadingIndicator.className = 'path-loading-indicator';
            this.pathLoadingIndicator.style.cssText = `
                position: fixed;
                top: 80px;
                right: 20px;
                background: rgba(0, 0, 0, 0.8);
                color: white;
                padding: 12px 20px;
                border-radius: 8px;
                font-size: 14px;
                z-index: 10000;
                box-shadow: 0 2px 8px rgba(0,0,0,0.3);
            `;
            document.body.appendChild(this.pathLoadingIndicator);
        }

        this.pathLoadingIndicator.textContent = `Loading paths: 0 / ${total}`;
        this.pathLoadingIndicator.style.display = 'block';
    }

    /**
     * Update path loading progress
     * @param {number} current - Current number of paths loaded
     */
    updatePathLoadingIndicator(current) {
        if (this.pathLoadingIndicator) {
            const total = this.pathLoadingIndicator.textContent.split('/')[1].trim();
            this.pathLoadingIndicator.textContent = `Loading paths: ${current} / ${total}`;
        }
    }

    /**
     * Hide path loading indicator
     */
    hidePathLoadingIndicator() {
        if (this.pathLoadingIndicator) {
            this.pathLoadingIndicator.style.display = 'none';
        }
    }

    /**
     * Clear all paths layer
     */
    clearAllPaths() {
        if (this.allPathsLayer) {
            this.allPathsLayer.clearLayers();
            this.map.removeLayer(this.allPathsLayer);
            this.allPathsLayer = null;
            this.showingAllPaths = false;
            console.log('Cleared all paths');
        }
    }

    /**
     * Show local coverage (zero-hop) when clicking MYCALL station
     */
    async showClickedLocalCoverage() {
        console.log('showClickedLocalCoverage called, current state:', {
            clickedLocalCoverage: this.clickedLocalCoverage,
            hasLocalPosition: !!this.localStationPosition,
            localPosition: this.localStationPosition
        });

        // If already showing, toggle it off
        if (this.clickedLocalCoverage) {
            console.log('Toggling off local coverage');
            this.clearClickedLocalCoverage();
            return;
        }

        // Check for GPS position
        if (!this.localStationPosition) {
            console.warn('My Coverage (Direct): No GPS position available');
            alert('No GPS position available. Make sure GPS is locked.');
            return;
        }

        try {
            // Fetch all stations to calculate coverage
            const response = await fetch('/api/stations');
            if (!response.ok) {
                console.error('Failed to fetch stations for local coverage');
                return;
            }

            const data = await response.json();
            const stations = data.stations;

            // Filter to zero-hop stations with valid positions
            const zeroHopStations = stations.filter(s =>
                s.heard_zero_hop === true &&
                s.has_position &&
                s.last_position &&
                this.isValidPosition(s.last_position.latitude, s.last_position.longitude)
            );

            if (zeroHopStations.length === 0) {
                console.warn('My Coverage (Direct): No zero-hop stations');
                return;
            }

            console.log(`My Coverage (Direct): ${zeroHopStations.length} zero-hop stations`);

            // Mark as active
            this.clickedLocalCoverage = true;

            // Draw coverage polygon (reuse existing local coverage layer)
            const localLatLng = [this.localStationPosition.lat, this.localStationPosition.lon];
            const stationPoints = [];
            let maxDistance = 0;

            zeroHopStations.forEach(station => {
                const stationLatLng = [station.last_position.latitude, station.last_position.longitude];
                stationPoints.push(stationLatLng);

                const distance = L.latLng(localLatLng).distanceTo(stationLatLng);
                if (distance > maxDistance) {
                    maxDistance = distance;
                }
            });

            // Create coverage layer if not exists
            if (this.localCoverageLayer) {
                this.localCoverageLayer.clearLayers();
            } else {
                this.localCoverageLayer = L.layerGroup().addTo(this.map);
            }

            // Calculate convex hull
            const allPoints = [localLatLng, ...stationPoints];
            const hullPoints = this.convexHull(allPoints);

            // Create coverage polygon with enhanced visibility
            const polygon = L.polygon(hullPoints, {
                color: '#ff0000',
                fillColor: '#ff0000',
                fillOpacity: 0.12,  // Slightly more visible when clicked
                weight: 3,  // Thicker border
                dashArray: '5, 5'
            });

            const popupContent = `
                <div class="coverage-popup">
                    <strong>My Coverage (Direct)</strong><br>
                    <small>Max range: ${(maxDistance / 1000).toFixed(1)} km</small><br>
                    <small>${zeroHopStations.length} stations (zero hops)</small><br>
                    <small style="color: #ffa500;">📡 Showing only zero-hop stations</small><br>
                    <small>Click again to show all</small>
                </div>
            `;
            polygon.bindPopup(popupContent);
            polygon.addTo(this.localCoverageLayer);

            // Filter stations to only show zero-hop stations
            this.filterStationsForLocalCoverage(zeroHopStations);

            console.log(`Showing local coverage with ${zeroHopStations.length} zero-hop stations`);
        } catch (error) {
            console.error('Failed to show local coverage:', error);
            this.clearClickedLocalCoverage();
        }
    }

    /**
     * Filter visible stations to only show zero-hop stations
     * @param {Array} zeroHopStations - Array of station objects with heard_zero_hop=true
     */
    filterStationsForLocalCoverage(zeroHopStations) {
        // Create set of zero-hop callsigns
        const zeroHopCallsigns = new Set(zeroHopStations.map(s => s.callsign));
        zeroHopCallsigns.add(this.mycall); // Always show MYCALL

        // Hide all stations not in zero-hop set
        this.hiddenStations.clear();
        Object.keys(this.markers).forEach(callsign => {
            if (!zeroHopCallsigns.has(callsign)) {
                const marker = this.markers[callsign];
                if (marker) {
                    this.map.removeLayer(marker);
                    this.hiddenStations.add(callsign);
                }
            }
        });

        console.log(`Filtered map to ${zeroHopCallsigns.size} zero-hop stations (hid ${this.hiddenStations.size})`);
    }

    /**
     * Clear clicked local coverage
     */
    clearClickedLocalCoverage() {
        if (this.clickedLocalCoverage) {
            // Clear coverage layer
            if (this.localCoverageLayer) {
                this.localCoverageLayer.clearLayers();
                this.map.removeLayer(this.localCoverageLayer);
                this.localCoverageLayer = null;
            }

            // Restore hidden stations
            this.restoreHiddenStations();

            this.clickedLocalCoverage = false;
            console.log('Cleared local coverage');
        }
    }

    /**
     * Update local station position from GPS
     * @param {Object} gpsData - GPS position data
     */
    updateLocalStation(gpsData) {
        if (!gpsData || !gpsData.locked || !gpsData.latitude || !gpsData.longitude) {
            // No GPS lock - remove marker if exists
            if (this.localStationMarker) {
                this.map.removeLayer(this.localStationMarker);
                this.localStationMarker = null;
                this.localStationPosition = null;
            }
            return;
        }

        const latLng = [gpsData.latitude, gpsData.longitude];
        this.localStationPosition = { lat: gpsData.latitude, lon: gpsData.longitude };

        // Create or update marker
        if (this.localStationMarker) {
            this.localStationMarker.setLatLng(latLng);

            // Update popup content
            const altStr = gpsData.altitude !== null && gpsData.altitude !== undefined
                ? `${gpsData.altitude}m (${Math.round(gpsData.altitude * 3.28084)}ft)`
                : 'N/A';

            const popupContent = `
                <div class="local-station-popup">
                    <strong>📍 My Position</strong><br>
                    <small>GPS Lock: ${gpsData.locked ? '✓' : '✗'}</small><br>
                    <small>Lat: ${gpsData.latitude.toFixed(6)}°</small><br>
                    <small>Lon: ${gpsData.longitude.toFixed(6)}°</small><br>
                    <small>Alt: ${altStr}</small><br>
                    <small style="color: #00aaff;">Click to show direct coverage</small>
                </div>
            `;
            this.localStationMarker.setPopupContent(popupContent);
        } else {
            // Create distinct marker for local station (home icon)
            const icon = L.divIcon({
                className: 'local-station-marker',
                html: `<div style="width:32px;height:32px;background:radial-gradient(circle, #ff0000 0%, #ff0000 40%, transparent 70%);border-radius:50%;border:3px solid white;box-shadow:0 2px 4px rgba(0,0,0,0.3);display:flex;align-items:center;justify-content:center;"><div style="width:12px;height:12px;background:white;border-radius:50%;"></div></div>`,
                iconSize: [32, 32],
                iconAnchor: [16, 16],
                popupAnchor: [0, -16]
            });

            this.localStationMarker = L.marker(latLng, {
                icon: icon,
                title: 'My Position (GPS)',
                zIndexOffset: 1000 // Always on top
            });

            // Create popup with GPS info
            const altStr = gpsData.altitude !== null && gpsData.altitude !== undefined
                ? `${gpsData.altitude}m (${Math.round(gpsData.altitude * 3.28084)}ft)`
                : 'N/A';

            const popupContent = `
                <div class="local-station-popup">
                    <strong>📍 My Position</strong><br>
                    <small>GPS Lock: ${gpsData.locked ? '✓' : '✗'}</small><br>
                    <small>Lat: ${gpsData.latitude.toFixed(6)}°</small><br>
                    <small>Lon: ${gpsData.longitude.toFixed(6)}°</small><br>
                    <small>Alt: ${altStr}</small><br>
                    <small style="color: #00aaff;">Click to show direct coverage</small>
                </div>
            `;
            this.localStationMarker.bindPopup(popupContent);

            // Add click event to show local coverage
            this.localStationMarker.on('click', async () => {
                console.log('Local station marker clicked');
                await this.showClickedLocalCoverage();
            });

            this.localStationMarker.addTo(this.map);
        }
    }

    /**
     * Get local station position (for radius filtering)
     * @returns {Object|null} Position {lat, lon} or null if no GPS lock
     */
    getLocalPosition() {
        return this.localStationPosition;
    }

    /**
     * Calculate distance between two points using Haversine formula
     * @param {number} lat1 - First point latitude
     * @param {number} lon1 - First point longitude
     * @param {number} lat2 - Second point latitude
     * @param {number} lon2 - Second point longitude
     * @returns {number} Distance in kilometers
     */
    static calculateDistance(lat1, lon1, lat2, lon2) {
        const R = 6371; // Earth's radius in km
        const dLat = (lat2 - lat1) * Math.PI / 180;
        const dLon = (lon2 - lon1) * Math.PI / 180;
        const a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
                  Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
                  Math.sin(dLon / 2) * Math.sin(dLon / 2);
        const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
        return R * c;
    }

    /**
     * Toggle local station coverage visualization
     * @param {boolean} show - Whether to show coverage
     * @param {Array} stations - All stations data (for calculating radius)
     * @param {string} timeRange - Time range filter ('all', '1h', '4h', '24h')
     */
    async toggleLocalCoverage(show, stations, timeRange = 'all') {
        this.showLocalCoverage = show;

        // Clear station path and clicked local coverage when using checkbox
        this.clearStationPath();
        this.clickedLocalCoverage = false; // Reset clicked state (checkbox takes over)

        if (show) {
            await this.updateLocalCoverage(stations, timeRange);
        } else {
            this.clearLocalCoverage();
        }
    }

    /**
     * Update local station coverage circle based on direct-heard stations
     * @param {Array} stations - All stations data
     * @param {string} timeRange - Time range filter ('all', '1h', '4h', '24h')
     */
    updateLocalCoverage(stations, timeRange = 'all') {
        console.log('updateLocalCoverage called:', {
            hasLocalPosition: !!this.localStationPosition,
            localPosition: this.localStationPosition,
            stationsCount: stations ? stations.length : 0
        });

        if (!this.localStationPosition) {
            // No GPS lock - can't show coverage
            console.warn('My Coverage (Direct): No GPS position available - cannot draw coverage circle');
            this.clearLocalCoverage();
            return;
        }

        if (!stations || stations.length === 0) {
            console.warn('My Coverage (Direct): No stations data available');
            this.clearLocalCoverage();
            return;
        }

        // Calculate cutoff time based on filter
        let cutoffTime = null;
        if (timeRange !== 'all') {
            const now = new Date();
            switch (timeRange) {
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
        }

        // Filter to zero-hop stations only (heard_zero_hop === true)
        // This shows our TRUE direct coverage - stations we've heard with NO digipeaters
        let zeroHopStations = stations.filter(s =>
            s.heard_zero_hop === true &&
            s.has_position &&
            s.last_position &&
            this.isValidPosition(s.last_position.latitude, s.last_position.longitude)  // Exclude Null Island
        );

        // Apply time filter if specified
        if (cutoffTime) {
            zeroHopStations = zeroHopStations.filter(s => {
                if (!s.last_heard) return false;
                const stationTime = new Date(s.last_heard);
                return stationTime >= cutoffTime;
            });
        }

        console.log(`My Coverage (Direct): Found ${zeroHopStations.length} stations with zero hops (time range: ${timeRange})`);

        if (zeroHopStations.length === 0) {
            // No zero-hop stations with valid positions
            console.warn('My Coverage (Direct): No zero-hop stations with valid positions');
            this.clearLocalCoverage();
            return;
        }

        // Collect all zero-hop station positions
        const localLatLng = [this.localStationPosition.lat, this.localStationPosition.lon];
        const stationPoints = [];
        let maxDistance = 0;

        zeroHopStations.forEach(station => {
            const stationLatLng = [station.last_position.latitude, station.last_position.longitude];
            stationPoints.push(stationLatLng);

            // Track max distance for popup display
            const distance = L.latLng(localLatLng).distanceTo(stationLatLng);
            if (distance > maxDistance) {
                maxDistance = distance;
            }
        });

        // Clear existing coverage
        this.clearLocalCoverage();

        // Create new layer group
        this.localCoverageLayer = L.layerGroup().addTo(this.map);

        // Add local station position to the point set for hull calculation
        const allPoints = [localLatLng, ...stationPoints];

        // Calculate convex hull for coverage polygon
        const hullPoints = this.convexHull(allPoints);

        // Create coverage polygon with subtle fill
        const polygon = L.polygon(hullPoints, {
            color: '#ff0000',
            fillColor: '#ff0000',
            fillOpacity: 0.08,
            weight: 2,
            dashArray: '5, 5'
        });

        // Add popup with coverage info
        const popupContent = `
            <div class="coverage-popup">
                <strong>My Coverage (Direct)</strong><br>
                <small>Max range: ${(maxDistance / 1000).toFixed(1)} km</small><br>
                <small>${zeroHopStations.length} stations (zero hops)</small>
            </div>
        `;
        polygon.bindPopup(popupContent);

        // Add to layer group
        polygon.addTo(this.localCoverageLayer);

        console.log(`My Coverage (Direct): ${(maxDistance / 1000).toFixed(1)} km to furthest of ${zeroHopStations.length} zero-hop stations`);
    }

    /**
     * Clear local coverage layer
     */
    clearLocalCoverage() {
        if (this.localCoverageLayer) {
            this.localCoverageLayer.clearLayers();
            this.map.removeLayer(this.localCoverageLayer);
            this.localCoverageLayer = null;
        }
    }
}
