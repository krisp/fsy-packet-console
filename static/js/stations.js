/**
 * APRS Stations - Station list and detail page logic
 */

import { APRSApi } from './api.js';
import { APRSMap } from './map.js';
import { createTemperatureChart } from './charts.js';
import { formatRelativeTime, escapeHtml } from './utils.js';

const api = new APRSApi();

const PATH_COLOR_OLD = '#1a4499';
const PATH_COLOR_NEW = '#00e5ff';

/**
 * Interpolate between two hex colors
 * @param {string} hex1 - Start color (e.g. '#1a4499')
 * @param {string} hex2 - End color (e.g. '#00e5ff')
 * @param {number} ratio - 0.0 (start) to 1.0 (end)
 * @returns {string} Interpolated hex color
 */
function interpolateColor(hex1, hex2, ratio) {
    const r1 = parseInt(hex1.slice(1, 3), 16);
    const g1 = parseInt(hex1.slice(3, 5), 16);
    const b1 = parseInt(hex1.slice(5, 7), 16);
    const r2 = parseInt(hex2.slice(1, 3), 16);
    const g2 = parseInt(hex2.slice(3, 5), 16);
    const b2 = parseInt(hex2.slice(5, 7), 16);
    const r = Math.round(r1 + (r2 - r1) * ratio);
    const g = Math.round(g1 + (g2 - g1) * ratio);
    const b = Math.round(b1 + (b2 - b1) * ratio);
    return `#${r.toString(16).padStart(2, '0')}${g.toString(16).padStart(2, '0')}${b.toString(16).padStart(2, '0')}`;
}

/**
 * Get cutoff Date for a time filter string
 * @param {string} timeFilter - '1h', '4h', '24h', or 'all'
 * @returns {Date|null}
 */
function getDetailTimeFilterCutoff(timeFilter) {
    if (!timeFilter || timeFilter === 'all') return null;
    const hours = { '1h': 1, '4h': 4, '24h': 24 }[timeFilter];
    return hours ? new Date(Date.now() - hours * 60 * 60 * 1000) : null;
}

/**
 * Render station movement path with gradient coloring onto a Leaflet LayerGroup.
 * Older segments are dark blue, newer segments are bright cyan.
 * @param {Object} leafletMap - Leaflet map instance
 * @param {Array} allPositions - Full position history array
 * @param {string} callsign - Station callsign (for popups)
 * @param {string} timeFilter - '1h', '4h', '24h', or 'all'
 * @param {Object} layerGroup - Leaflet LayerGroup to render into
 * @returns {Object|null} Leaflet LatLngBounds of rendered path, or null if no points
 */
function renderStationPath(leafletMap, allPositions, callsign, timeFilter, layerGroup) {
    layerGroup.clearLayers();

    if (!allPositions || allPositions.length < 2) return null;

    let validPositions = allPositions.filter(p =>
        !(p.latitude === 0.0 && p.longitude === 0.0)
    );

    const cutoff = getDetailTimeFilterCutoff(timeFilter);
    if (cutoff) {
        validPositions = validPositions.filter(p => new Date(p.timestamp) >= cutoff);
    }

    if (validPositions.length < 2) return null;

    const sorted = validPositions.slice().sort((a, b) =>
        new Date(a.timestamp) - new Date(b.timestamp)
    );
    const n = sorted.length;
    const firstTime = new Date(sorted[0].timestamp);
    const lastTime = new Date(sorted[n - 1].timestamp);

    // Draw gradient-colored polyline segments (old = dark blue → new = bright cyan)
    for (let i = 0; i < n - 1; i++) {
        const ratio = n > 2 ? i / (n - 2) : 1;
        const color = interpolateColor(PATH_COLOR_OLD, PATH_COLOR_NEW, ratio);
        L.polyline(
            [[sorted[i].latitude, sorted[i].longitude],
             [sorted[i + 1].latitude, sorted[i + 1].longitude]],
            { color, weight: 3, opacity: 0.4 + ratio * 0.5, smoothFactor: 1 }
        ).addTo(layerGroup);
    }

    // Add circle markers for each historical position (skip most recent — shown as station marker)
    sorted.forEach((pos, index) => {
        if (index === n - 1) return;
        const ratio = n > 2 ? index / (n - 2) : 0;
        const color = interpolateColor(PATH_COLOR_OLD, PATH_COLOR_NEW, ratio);
        const fillOpacity = 0.3 + ratio * 0.5;
        const ageMin = Math.round((lastTime - new Date(pos.timestamp)) / (1000 * 60));
        const timeStr = new Date(pos.timestamp).toLocaleString();
        L.circleMarker([pos.latitude, pos.longitude], {
            radius: 4,
            fillColor: color,
            color: '#fff',
            weight: 1,
            opacity: Math.min(fillOpacity + 0.2, 1.0),
            fillOpacity
        }).bindPopup(`
            <div class="path-popup">
                <strong>${escapeHtml(callsign)}</strong><br>
                <small>${timeStr}</small><br>
                <small>${ageMin} minutes ago</small><br>
                <small>${pos.latitude.toFixed(6)}, ${pos.longitude.toFixed(6)}</small>
                ${pos.grid_square ? `<br><small>Grid: ${escapeHtml(pos.grid_square)}</small>` : ''}
            </div>
        `).addTo(layerGroup);
    });

    // Invisible wide polyline for click-to-popup on the path summary
    const timeSpan = ((lastTime - firstTime) / (1000 * 60 * 60)).toFixed(1);
    L.polyline(sorted.map(p => [p.latitude, p.longitude]), {
        color: '#000', weight: 10, opacity: 0.001
    }).bindPopup(`
        <div class="path-popup">
            <strong>${escapeHtml(callsign)} Movement Path</strong><br>
            <small>Positions: ${n}</small><br>
            <small>Time span: ${timeSpan} hours</small><br>
            <small>Oldest: ${firstTime.toLocaleString()}</small><br>
            <small>Newest: ${lastTime.toLocaleString()}</small>
        </div>
    `).addTo(layerGroup);

    return L.latLngBounds(sorted.map(p => [p.latitude, p.longitude]));
}

/**
 * Render station list in sidebar
 * @param {Array} stations - Array of station objects
 */
export function renderStationList(stations) {
    const container = document.getElementById('stations');
    if (!container) return;

    if (stations.length === 0) {
        container.innerHTML = '<p class="empty-message">No stations heard yet</p>';
        return;
    }

    container.innerHTML = stations.map(station => {
        const lastHeard = formatRelativeTime(station.last_heard);
        const position = station.has_position ? station.last_position.grid_square : 'No position';
        const hopInfo = station.hop_count !== null && station.hop_count !== 999
            ? `${station.hop_count} hops`
            : (station.heard_direct ? 'Direct' : 'Relayed');

        return `
            <div class="station-item" onclick="window.location.href='/station/${encodeURIComponent(station.callsign)}'">
                <div class="station-callsign">${escapeHtml(station.callsign)}</div>
                <div class="station-info">${escapeHtml(position)}</div>
                <div class="station-meta">
                    ${lastHeard} • ${station.packets_heard} packets • ${hopInfo}
                </div>
            </div>
        `;
    }).join('');
}

/**
 * Render weather station list in sidebar
 * @param {Array} weatherStations - Array of station objects with weather
 */
export function renderWeatherList(weatherStations) {
    const container = document.getElementById('weather-stations');
    if (!container) return;

    if (weatherStations.length === 0) {
        container.innerHTML = '<p class="empty-message">No weather stations</p>';
        return;
    }

    container.innerHTML = weatherStations.map(station => {
        const wx = station.last_weather;
        const lastHeard = formatRelativeTime(station.last_heard);

        let tempStr = wx.temperature !== null ? `${wx.temperature}°F` : '-';
        let humidityStr = wx.humidity !== null ? `${wx.humidity}%` : '-';
        let windStr = wx.wind_speed !== null ? `${wx.wind_speed} mph` : '-';
        let pressureStr = wx.pressure !== null ? `${wx.pressure} mbar` : '-';

        return `
            <div class="weather-item" onclick="window.location.href='/station/${encodeURIComponent(station.callsign)}'">
                <div class="weather-callsign">${escapeHtml(station.callsign)}</div>
                <div class="weather-data">
                    <div>Temp: <span class="weather-value">${tempStr}</span></div>
                    <div>Humidity: <span class="weather-value">${humidityStr}</span></div>
                    <div>Wind: <span class="weather-value">${windStr}</span></div>
                    <div>Pressure: <span class="weather-value">${pressureStr}</span></div>
                </div>
                <div class="station-meta">${lastHeard}</div>
            </div>
        `;
    }).join('');
}

/**
 * Render message list in sidebar
 * @param {Array} messages - Array of message objects
 */
export function renderMessageList(messages) {
    const container = document.getElementById('message-list');
    if (!container) return;

    if (messages.length === 0) {
        container.innerHTML = '<p class="empty-message">No messages received</p>';
        return;
    }

    // Show only recent 10 messages
    const recentMessages = messages.slice(0, 10);

    container.innerHTML = recentMessages.map(msg => {
        const unreadClass = msg.read ? '' : 'unread';
        const time = formatRelativeTime(msg.timestamp);

        return `
            <div class="message-item ${unreadClass}">
                <div class="message-header">
                    <span class="message-from callsign-link" onclick="window.location.href='/station/${encodeURIComponent(msg.from_call)}'">${escapeHtml(msg.from_call)}</span>
                    <span class="message-time">${time}</span>
                </div>
                <div class="message-text">${escapeHtml(msg.message)}</div>
            </div>
        `;
    }).join('');
}

/**
 * Render station message history
 * @param {string} callsign - Station callsign
 * @param {Array} messages - Array of message objects
 */
export function renderStationMessages(callsign, messages) {
    // Handle empty state
    if (messages.length === 0) {
        return '<p class="empty-message">No messages with this station</p>';
    }

    // Messages already sorted by backend (newest first)
    // Render with direction indicators and highlighting
    return messages.map(msg => {
        const isSent = msg.from_call === callsign;
        const directionClass = isSent ? 'sent' : 'received';

        // Make callsigns clickable if they're not the current station
        const fromCallHtml = msg.from_call === callsign
            ? `<span class="station-highlight">${escapeHtml(msg.from_call)}</span>`
            : `<span class="callsign-link" onclick="window.location.href='/station/${encodeURIComponent(msg.from_call)}'">${escapeHtml(msg.from_call)}</span>`;

        const toCallHtml = msg.to_call === callsign
            ? `<span class="station-highlight">${escapeHtml(msg.to_call)}</span>`
            : `<span class="callsign-link" onclick="window.location.href='/station/${encodeURIComponent(msg.to_call)}'">${escapeHtml(msg.to_call)}</span>`;

        return `
            <div class="station-message-item ${directionClass}">
                <div class="station-message-header">
                    <div class="station-message-route">
                        ${fromCallHtml}
                        <span>→</span>
                        ${toCallHtml}
                    </div>
                    <div class="station-message-time">${formatRelativeTime(msg.timestamp)}</div>
                </div>
                <div class="station-message-text">${escapeHtml(msg.message)}</div>
            </div>
        `;
    }).join('');
}

/**
 * Load and display station detail page
 * @param {string} callsign - Station callsign
 */
export async function loadStationDetail(callsign) {
    try {
        const station = await api.getStation(callsign);
        console.log('Station data:', station);
        console.log('Position history count:', station.position_history ? station.position_history.length : 0);

        // Update page title and callsign
        document.title = `${callsign} - FSY Packet Console`;
        document.getElementById('callsign').textContent = callsign;

        // Update last heard
        document.getElementById('last-heard').textContent = `Last heard: ${formatRelativeTime(station.last_heard)}`;

        // Update position information
        if (station.has_position && station.last_position) {
            const pos = station.last_position;

            // Filter out invalid "Null Island" coordinates
            const isValidPosition = !(pos.latitude === 0.0 && pos.longitude === 0.0);

            if (isValidPosition) {
                document.getElementById('grid-square').textContent = pos.grid_square || '-';
                document.getElementById('latitude').textContent = pos.latitude.toFixed(6);
                document.getElementById('longitude').textContent = pos.longitude.toFixed(6);
                document.getElementById('altitude').textContent = pos.altitude !== null ? `${pos.altitude} ft` : '-';

                // Display APRS symbol using sprite
                const symbolElement = document.getElementById('symbol');
                const isPrimary = pos.symbol_table === '/';
                const codeNum = pos.symbol_code.charCodeAt(0) - 33; // ASCII offset from '!'
                const col = codeNum % 16;
                const row = Math.floor(codeNum / 16) + (isPrimary ? 0 : 6);
                const x = col * 24;
                const y = row * 24;

                symbolElement.innerHTML = `
                    <div style="width:24px;height:24px;background-image:url(/static/vendor/aprs-symbols.png);background-position:-${x}px -${y}px;background-repeat:no-repeat;display:inline-block;"></div>
                `;

                document.getElementById('comment').textContent = pos.comment || '-';

                // Create map centered on station (without auto-save to preserve main map state)
                const map = new APRSMap('station-map', { autoSave: false });
                map.addOrUpdateStation(station);

                // Set up path rendering with time filter
                const pathLayer = L.layerGroup().addTo(map.map);

                if (station.has_path && station.position_history) {
                    document.getElementById('path-time-controls').style.display = '';

                    // Initial render with default 24h filter
                    const initialBounds = renderStationPath(
                        map.map, station.position_history, callsign, '24h', pathLayer
                    );
                    if (initialBounds) {
                        map.map.fitBounds(initialBounds.pad(0.1), { padding: [40, 40] });
                    } else {
                        map.map.setView([pos.latitude, pos.longitude], 12);
                    }

                    // Re-render path when time filter changes (don't re-fit bounds)
                    document.getElementById('path-time-filter').addEventListener('change', (e) => {
                        renderStationPath(
                            map.map, station.position_history, callsign, e.target.value, pathLayer
                        );
                    });
                } else {
                    map.map.setView([pos.latitude, pos.longitude], 12);
                }

            // Check if this station is a digipeater and show coverage
            try {
                const digiResponse = await fetch(`/api/digipeaters/${encodeURIComponent(callsign)}`);
                if (digiResponse.ok) {
                    const digiData = await digiResponse.json();

                    // Draw coverage polygon if digipeater has coverage data
                    if (digiData.has_position && digiData.position && digiData.stations_heard && digiData.stations_heard.length > 0) {
                        const digiLatLng = [digiData.position.latitude, digiData.position.longitude];

                        // Collect all valid station positions
                        const stationPoints = [];
                        let maxDistance = 0;

                        const stationsWithPos = digiData.stations_heard.filter(s =>
                            s.position &&
                            !(s.position.latitude === 0.0 && s.position.longitude === 0.0)
                        );

                        stationsWithPos.forEach(heardStation => {
                            const stationLatLng = [heardStation.position.latitude, heardStation.position.longitude];
                            stationPoints.push(stationLatLng);

                            // Track max distance for popup display
                            const distance = L.latLng(digiLatLng).distanceTo(stationLatLng);
                            if (distance > maxDistance) {
                                maxDistance = distance;
                            }
                        });

                        // Draw coverage polygon if we have stations
                        if (maxDistance > 0) {
                            // Add digipeater position to the point set for hull calculation
                            const allPoints = [digiLatLng, ...stationPoints];

                            // Calculate convex hull for coverage polygon
                            const hullPoints = map.convexHull(allPoints);

                            // Create coverage polygon with light blue fill
                            const polygon = L.polygon(hullPoints, {
                                color: '#3388ff',
                                fillColor: '#3388ff',
                                fillOpacity: 0.08,
                                weight: 2,
                                dashArray: '5, 5'
                            });

                            // Add popup with coverage info (but don't open it)
                            const popupContent = `
                                <div class="digipeater-popup">
                                    <strong><span class="callsign-link" onclick="window.location.href='/station/${encodeURIComponent(callsign)}'">${escapeHtml(callsign)}</span> Coverage Area</strong><br>
                                    <small>Max range: ${(maxDistance / 1000).toFixed(1)} km (${(maxDistance / 1609.34).toFixed(1)} mi)</small><br>
                                    <small>Stations heard: ${digiData.station_count}</small>
                                </div>
                            `;
                            polygon.bindPopup(popupContent);
                            polygon.addTo(map.map);

                            // Plot all stations heard directly by this digipeater
                            stationsWithPos.forEach(heardStation => {
                                // Determine marker color based on station type
                                let fillColor;
                                if (heardStation.has_weather && heardStation.is_digipeater) {
                                    fillColor = '#ff8c00';  // Orange for weather + digipeater
                                } else if (heardStation.has_weather) {
                                    fillColor = '#ffd700';  // Yellow for weather stations
                                } else if (heardStation.is_digipeater) {
                                    fillColor = '#3388ff';  // Blue for digipeaters
                                } else {
                                    fillColor = '#4CAF50';  // Green for end stations
                                }

                                // Create a simple marker for each station
                                const stationMarker = L.circleMarker(
                                    [heardStation.position.latitude, heardStation.position.longitude],
                                    {
                                        radius: 6,
                                        fillColor: fillColor,
                                        color: '#fff',
                                        weight: 2,
                                        opacity: 1,
                                        fillOpacity: 0.8
                                    }
                                );

                                // Add popup with station info
                                const stationPopup = `
                                    <div class="station-popup">
                                        <strong><span class="callsign-link" onclick="window.location.href='/station/${encodeURIComponent(heardStation.callsign)}'">${escapeHtml(heardStation.callsign)}</span></strong><br>
                                        <small>Grid: ${escapeHtml(heardStation.position.grid_square || 'N/A')}</small><br>
                                        <small>Distance: ${(L.latLng(digiLatLng).distanceTo([heardStation.position.latitude, heardStation.position.longitude]) / 1000).toFixed(1)} km</small>
                                    </div>
                                `;
                                stationMarker.bindPopup(stationPopup);
                                stationMarker.addTo(map.map);
                            });

                            // Adjust map to show coverage area
                            const bounds = polygon.getBounds();
                            map.map.fitBounds(bounds, { padding: [50, 50] });
                        }
                    }
                }
            } catch (error) {
                // Not a digipeater or no coverage data - just ignore
                console.log('Station is not a digipeater or has no coverage data');
            }

            // Check if this is the local station (MYCALL) and show direct coverage
            try {
                const statusResponse = await fetch('/api/status');
                if (statusResponse.ok) {
                    const statusData = await statusResponse.json();
                    const mycall = statusData.mycall;

                    // If this station is MYCALL, show zero-hop coverage
                    if (callsign === mycall && pos) {
                        // Fetch all stations to calculate zero-hop coverage
                        const stationsResponse = await fetch('/api/stations');
                        if (stationsResponse.ok) {
                            const stationsData = await stationsResponse.json();

                            // Filter to zero-hop stations with valid positions
                            const zeroHopStations = stationsData.stations.filter(s =>
                                s.heard_zero_hop === true &&
                                s.has_position &&
                                s.last_position &&
                                s.last_position.latitude !== 0.0 &&
                                s.last_position.longitude !== 0.0
                            );

                            if (zeroHopStations.length > 0) {
                                const localLatLng = [pos.latitude, pos.longitude];

                                // Collect all zero-hop station positions
                                const stationPoints = [];
                                let maxDistance = 0;

                                zeroHopStations.forEach(zeroStation => {
                                    const stationLatLng = [
                                        zeroStation.last_position.latitude,
                                        zeroStation.last_position.longitude
                                    ];
                                    stationPoints.push(stationLatLng);

                                    // Track max distance for popup display
                                    const distance = L.latLng(localLatLng).distanceTo(stationLatLng);
                                    if (distance > maxDistance) {
                                        maxDistance = distance;
                                    }
                                });

                                // Draw coverage polygon
                                if (maxDistance > 0) {
                                    // Add local station position to the point set for hull calculation
                                    const allPoints = [localLatLng, ...stationPoints];

                                    // Calculate convex hull for coverage polygon
                                    const hullPoints = map.convexHull(allPoints);

                                    // Create coverage polygon with subtle fill
                                    const polygon = L.polygon(hullPoints, {
                                        color: '#ff0000',
                                        fillColor: '#ff0000',
                                        fillOpacity: 0.08,
                                        weight: 2,
                                        dashArray: '5, 5'
                                    });

                                    const popupContent = `
                                        <div class="coverage-popup">
                                            <strong>My Coverage (Direct)</strong><br>
                                            <small>Max range: ${(maxDistance / 1000).toFixed(1)} km (${(maxDistance / 1609.34).toFixed(1)} mi)</small><br>
                                            <small>${zeroHopStations.length} stations (zero hops)</small>
                                        </div>
                                    `;
                                    polygon.bindPopup(popupContent);
                                    polygon.addTo(map.map);

                                    // Plot all zero-hop stations
                                    zeroHopStations.forEach(zeroStation => {
                                        // Determine marker color based on station type
                                        let fillColor;
                                        if (zeroStation.has_weather && zeroStation.is_digipeater) {
                                            fillColor = '#ff8c00';  // Orange for weather + digipeater
                                        } else if (zeroStation.has_weather) {
                                            fillColor = '#ffd700';  // Yellow for weather stations
                                        } else if (zeroStation.is_digipeater) {
                                            fillColor = '#3388ff';  // Blue for digipeaters
                                        } else {
                                            fillColor = '#4CAF50';  // Green for end stations
                                        }

                                        const stationMarker = L.circleMarker(
                                            [zeroStation.last_position.latitude, zeroStation.last_position.longitude],
                                            {
                                                radius: 6,
                                                fillColor: fillColor,
                                                color: '#fff',
                                                weight: 2,
                                                opacity: 1,
                                                fillOpacity: 0.8
                                            }
                                        );

                                        const stationPopup = `
                                            <div class="station-popup">
                                                <strong><span class="callsign-link" onclick="window.location.href='/station/${encodeURIComponent(zeroStation.callsign)}'">${escapeHtml(zeroStation.callsign)}</span></strong><br>
                                                <small>Grid: ${escapeHtml(zeroStation.last_position.grid_square || 'N/A')}</small><br>
                                                <small>Distance: ${(L.latLng(localLatLng).distanceTo([zeroStation.last_position.latitude, zeroStation.last_position.longitude]) / 1000).toFixed(1)} km</small>
                                            </div>
                                        `;
                                        stationMarker.bindPopup(stationPopup);
                                        stationMarker.addTo(map.map);
                                    });

                                    // Adjust map to show coverage area
                                    const bounds = polygon.getBounds();
                                    map.map.fitBounds(bounds, { padding: [50, 50] });
                                }
                            }
                        }
                    }
                }
            } catch (error) {
                // Not local station or error fetching data - just ignore
                console.log('Not local station or error fetching coverage data:', error);
            }
            } // End isValidPosition check
        }

        // Update statistics
        document.getElementById('first-heard').textContent = formatRelativeTime(station.first_heard);
        document.getElementById('last-heard-stat').textContent = formatRelativeTime(station.last_heard);

        // Update device info if available
        if (station.device) {
            document.getElementById('device').textContent = station.device;
            document.getElementById('device-item').style.display = '';
        } else {
            document.getElementById('device-item').style.display = 'none';
        }

        document.getElementById('packets-heard').textContent = station.packets_heard;
        document.getElementById('messages-received').textContent = station.messages_received;
        document.getElementById('messages-sent').textContent = station.messages_sent;
        document.getElementById('hop-count').textContent = station.hop_count !== null && station.hop_count !== 999
            ? station.hop_count
            : '-';
        document.getElementById('heard-direct').textContent = station.heard_direct ? 'Yes' : 'No';
        document.getElementById('heard-zero-hop').textContent = station.heard_zero_hop ? 'Yes' : 'No';

        // Update digipeater paths (all unique paths observed)
        const pathsElement = document.getElementById('digipeater-paths');
        if (station.digipeater_paths && station.digipeater_paths.length > 0) {
            // Helper to check if a callsign is a generic alias
            const isGenericAlias = (call) => {
                const upper = call.toUpperCase();
                if (/^WIDE\d/.test(upper)) return true;  // WIDE1, WIDE2, etc.
                if (/^[A-Z]{2}\d$/.test(upper)) return true;  // CT2, MA2, etc.
                if (upper === 'RELAY' || upper === 'TRACE') return true;
                return false;
            };

            // Format each path with clickable callsigns
            const formattedPaths = station.digipeater_paths.map(path => {
                if (path.length === 1 && path[0] === 'DIRECT') {
                    return 'DIRECT (0-hop)';
                }
                // Filter out aliases, keep only real stations
                const realStations = path
                    .map(call => call.replace(/\*$/, ''))  // Remove * marker
                    .filter(call => !isGenericAlias(call));

                if (realStations.length > 0) {
                    // Make each callsign clickable
                    const clickableStations = realStations.map(call =>
                        `<span class="callsign-link" onclick="window.location.href='/station/${encodeURIComponent(call)}'">${escapeHtml(call)}</span>`
                    );
                    return clickableStations.join(' → ');
                } else {
                    return 'Via aliases only';
                }
            });

            pathsElement.innerHTML = formattedPaths.map(p => `<div>${p}</div>`).join('');
        } else {
            pathsElement.textContent = '-';
        }

        // Update weather section if available
        if (station.has_weather && station.last_weather) {
            const wx = station.last_weather;
            const weatherSection = document.getElementById('weather-history');
            weatherSection.style.display = 'block';

            document.getElementById('wx-temp').textContent = wx.temperature !== null ? `${wx.temperature}°F` : '-';
            document.getElementById('wx-humidity').textContent = wx.humidity !== null ? `${wx.humidity}%` : '-';
            document.getElementById('wx-pressure').textContent = wx.pressure !== null ? `${wx.pressure} mbar` : '-';

            let windText = '-';
            if (wx.wind_speed !== null) {
                windText = `${wx.wind_speed} mph`;
                if (wx.wind_direction !== null) {
                    windText += ` @ ${wx.wind_direction}°`;
                }
                if (wx.wind_gust !== null) {
                    windText += ` (gust ${wx.wind_gust} mph)`;
                }
            }
            document.getElementById('wx-wind').textContent = windText;

            // Fetch and display Zambretti forecast if pressure available
            if (wx.pressure !== null) {
                try {
                    const forecastResponse = await fetch(`/api/zambretti/${encodeURIComponent(callsign)}`);
                    if (forecastResponse.ok) {
                        const forecast = await forecastResponse.json();
                        const forecastDiv = document.getElementById('zambretti-forecast');
                        const forecastText = document.getElementById('forecast-text');
                        const forecastDetails = document.getElementById('forecast-details');

                        forecastText.textContent = `${forecast.forecast} (Code ${forecast.code})`;

                        const trendIcon = forecast.trend === 'rising' ? '↑' :
                                         forecast.trend === 'falling' ? '↓' : '→';
                        const confidenceColor = forecast.confidence === 'high' ? '#28a745' :
                                               forecast.confidence === 'medium' ? '#ffc107' : '#6c757d';

                        forecastDetails.innerHTML = `
                            Pressure ${trendIcon} ${forecast.trend}
                            <span style="color: ${confidenceColor}; font-weight: bold;">(${forecast.confidence} confidence)</span>
                        `;

                        forecastDiv.style.display = 'block';
                    }
                } catch (error) {
                    console.warn('Failed to fetch Zambretti forecast:', error);
                }
            }

            // Create temperature chart if history available
            if (station.weather_history && station.weather_history.length > 0) {
                createTemperatureChart('temp-chart', station.weather_history);
            }
        }

        // Load and display messages for this station
        try {
            const messageData = await api.getMonitoredMessages(null, callsign);
            const messagesSection = document.getElementById('station-messages');
            messagesSection.innerHTML = renderStationMessages(callsign, messageData.messages);
        } catch (error) {
            console.error('Failed to load station messages:', error);
            const messagesSection = document.getElementById('station-messages');
            messagesSection.innerHTML = '<p class="empty-message">Failed to load message history</p>';
        }

    } catch (error) {
        console.error('Failed to load station detail:', error);
        alert(`Failed to load station ${callsign}: ${error.message}`);
        window.location.href = '/';
    }
}
