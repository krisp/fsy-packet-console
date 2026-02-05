/**
 * Stations Table View - Sortable tabular display of all stations
 */

import { APRSApi } from './api.js';

let api = new APRSApi();
let stationsData = [];
let currentSort = { column: 'callsign', ascending: true };
let searchFilter = '';
let dxOnlyFilter = false;
let sse = null;

/**
 * Initialize the stations table page
 */
async function init() {
    // Load initial data
    await loadStations();
    await loadStatus();

    // Setup event listeners
    setupSortHandlers();
    setupSearchHandler();
    setupDXOnlyFilterHandler();
    setupRefreshHandler();
    setupMessageClickHandler();

    // Connect to SSE for live updates
    connectSSE();
}

/**
 * Setup message count click handler
 */
function setupMessageClickHandler() {
    const messageCount = document.getElementById('message-count');
    if (messageCount) {
        messageCount.addEventListener('click', () => {
            // Redirect to main page (which has the messages modal)
            window.location.href = '/';
        });
        messageCount.style.cursor = 'pointer';
    }
}

/**
 * Load stations from API
 */
async function loadStations() {
    try {
        const data = await api.getStations('last');
        stationsData = data.stations || [];
        renderTable();
        updateTableInfo();

        // Update station count in header based on loaded data
        const stationCount = stationsData.length;
        const stationCountEl = document.getElementById('station-count');
        if (stationCountEl) {
            stationCountEl.textContent = `${stationCount} Station${stationCount !== 1 ? 's' : ''}`;
        }
    } catch (error) {
        console.error('Failed to load stations:', error);
        showError('Failed to load stations');
    }
}

/**
 * Load system status
 */
async function loadStatus() {
    try {
        const status = await api.getStatus();
        document.getElementById('mycall').textContent = status.mycall || 'NOCALL';

        // Update station count
        const stationCount = status.station_count || 0;
        document.getElementById('station-count').textContent = `${stationCount} Station${stationCount !== 1 ? 's' : ''}`;

        // Update message count (messages to you / total monitored messages)
        const toYou = status.message_count || 0;
        const total = status.monitored_message_count || 0;
        document.getElementById('message-count').textContent = `${toYou}/${total} Messages`;

    } catch (error) {
        console.error('Failed to load status:', error);
    }
}

/**
 * Connect to SSE for real-time updates
 */
function connectSSE() {
    sse = api.connectSSE((eventType, data) => {
        if (eventType === 'station_update') {
            handleStationUpdate(data);
        }
        // Ignore other event types for this page
    });

    // Handle connection status
    sse.onopen = () => {
        handleConnectionChange(true);
    };

    sse.onerror = () => {
        handleConnectionChange(false);
    };
}

/**
 * Setup sort click handlers on table headers
 */
function setupSortHandlers() {
    const headers = document.querySelectorAll('th.sortable');
    headers.forEach(header => {
        header.addEventListener('click', () => {
            const column = header.dataset.sort;
            handleSort(column);
        });
    });
}

/**
 * Setup search input handler
 */
function setupSearchHandler() {
    const searchInput = document.getElementById('search-input');
    searchInput.addEventListener('input', (e) => {
        searchFilter = e.target.value.toLowerCase();
        renderTable();
        updateTableInfo();
    });
}

/**
 * Setup DX Only filter handler
 */
function setupDXOnlyFilterHandler() {
    const checkbox = document.getElementById('filter-dx-only');
    checkbox.addEventListener('change', (e) => {
        dxOnlyFilter = e.target.checked;
        renderTable();
        updateTableInfo();
    });
}

/**
 * Setup refresh button handler
 */
function setupRefreshHandler() {
    document.getElementById('refresh-btn').addEventListener('click', loadStations);
}

/**
 * Handle column sort
 */
function handleSort(column) {
    if (currentSort.column === column) {
        // Toggle direction
        currentSort.ascending = !currentSort.ascending;
    } else {
        // New column, default to ascending
        currentSort.column = column;
        currentSort.ascending = true;
    }

    // Update header indicators
    updateSortIndicators();

    // Re-render table
    renderTable();
}

/**
 * Update sort indicator arrows in headers
 */
function updateSortIndicators() {
    const headers = document.querySelectorAll('th.sortable');
    headers.forEach(header => {
        const indicator = header.querySelector('.sort-indicator');
        header.classList.remove('sorted-asc', 'sorted-desc');

        if (header.dataset.sort === currentSort.column) {
            if (currentSort.ascending) {
                header.classList.add('sorted-asc');
                indicator.textContent = '▲';
            } else {
                header.classList.add('sorted-desc');
                indicator.textContent = '▼';
            }
        } else {
            indicator.textContent = '';
        }
    });
}

/**
 * Get sort value for a station
 */
function getSortValue(station, column) {
    switch (column) {
        case 'callsign':
            return station.callsign.toLowerCase();
        case 'grid':
            return station.last_position?.grid_square || 'ZZZZZZ';
        case 'device':
            return (station.device || 'ZZZZZZ').toLowerCase();
        case 'last_heard':
            return new Date(station.last_heard).getTime();
        case 'packets':
            return station.packets_heard || 0;
        case 'hops':
            return station.hop_count || 999;
        case 'direct':
            return station.heard_direct ? 0 : 1; // Direct first
        case 'messages':
            return (station.messages_received || 0) + (station.messages_sent || 0);
        case 'comment':
            return (station.last_position?.comment || '').toLowerCase();
        case 'path':
            const path = station.digipeater_path || [];
            return path.join(',').toLowerCase();
        default:
            return '';
    }
}

/**
 * Sort and filter stations
 */
function getSortedFilteredStations() {
    let filtered = stationsData;

    // Apply search filter
    if (searchFilter) {
        filtered = filtered.filter(station =>
            station.callsign.toLowerCase().includes(searchFilter)
        );
    }

    // Apply DX Only filter (heard_zero_hop = true)
    if (dxOnlyFilter) {
        filtered = filtered.filter(station =>
            station.heard_zero_hop === true
        );
    }

    // Sort
    const sorted = [...filtered].sort((a, b) => {
        const aVal = getSortValue(a, currentSort.column);
        const bVal = getSortValue(b, currentSort.column);

        let comparison = 0;
        if (aVal < bVal) comparison = -1;
        if (aVal > bVal) comparison = 1;

        return currentSort.ascending ? comparison : -comparison;
    });

    return sorted;
}

/**
 * Render the stations table
 */
function renderTable() {
    const tbody = document.getElementById('stations-tbody');
    const stations = getSortedFilteredStations();

    if (stations.length === 0) {
        tbody.innerHTML = '<tr class="loading-row"><td colspan="10" class="loading-cell">No stations found</td></tr>';
        return;
    }

    tbody.innerHTML = stations.map(station => {
        const grid = station.last_position?.grid_square?.substring(0, 6) || '—';
        const device = escapeHtml(station.device || '—');
        const lastHeard = formatTimestamp(station.last_heard);
        const packets = station.packets_heard || 0;
        const hops = station.hop_count !== undefined ? station.hop_count : '—';
        const direct = station.heard_direct ? '✓' : '—';
        const messages = (station.messages_received || 0) + (station.messages_sent || 0);
        const comment = escapeHtml(station.last_position?.comment || '').substring(0, 50);
        const path = (station.digipeater_path || []).join(', ') || '—';

        return `
            <tr class="station-row" data-callsign="${escapeHtml(station.callsign)}">
                <td class="callsign-cell">
                    <a href="/station/${encodeURIComponent(station.callsign)}" class="callsign-link">
                        ${escapeHtml(station.callsign)}
                    </a>
                </td>
                <td>${grid}</td>
                <td class="device-cell">${device}</td>
                <td title="${escapeHtml(station.last_heard)}">${lastHeard}</td>
                <td class="numeric-cell">${packets}</td>
                <td class="numeric-cell">${hops}</td>
                <td class="center-cell">${direct}</td>
                <td class="numeric-cell">${messages || '—'}</td>
                <td class="comment-cell">${comment}</td>
                <td class="path-cell">${escapeHtml(path)}</td>
            </tr>
        `;
    }).join('');
}

/**
 * Update table footer info
 */
function updateTableInfo() {
    const filtered = getSortedFilteredStations();
    const total = stationsData.length;

    let info = `Showing ${filtered.length} of ${total} stations`;
    const activeFilters = [];

    if (searchFilter) {
        activeFilters.push(`"${searchFilter}"`);
    }
    if (dxOnlyFilter) {
        activeFilters.push('DX Only');
    }

    if (activeFilters.length > 0) {
        info += ` (filtered by ${activeFilters.join(', ')})`;
    }

    document.getElementById('table-info').textContent = info;
}

/**
 * Format timestamp for display
 */
function formatTimestamp(timestamp) {
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

    // Older than a week, show date
    const month = date.getMonth() + 1;
    const day = date.getDate();
    const hours = date.getHours().toString().padStart(2, '0');
    const mins = date.getMinutes().toString().padStart(2, '0');
    return `${month}/${day} ${hours}:${mins}`;
}

/**
 * Escape HTML to prevent XSS
 */
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/**
 * Handle SSE station update
 */
function handleStationUpdate(data) {
    // Find and update station in array
    const index = stationsData.findIndex(s => s.callsign === data.callsign);

    if (index >= 0) {
        // Update existing station
        stationsData[index] = { ...stationsData[index], ...data };
    } else {
        // New station
        stationsData.push(data);
    }

    // Re-render table
    renderTable();
    updateTableInfo();

    // Update station count in header
    const stationCount = stationsData.length;
    const stationCountEl = document.getElementById('station-count');
    if (stationCountEl) {
        stationCountEl.textContent = `${stationCount} Station${stationCount !== 1 ? 's' : ''}`;
    }
}

/**
 * Handle SSE connection status change
 */
function handleConnectionChange(connected) {
    const statusEl = document.getElementById('connection-status');
    if (connected) {
        statusEl.textContent = 'Connected';
        statusEl.className = 'status-item status-connected';
    } else {
        statusEl.textContent = 'Disconnected';
        statusEl.className = 'status-item status-disconnected';
    }
}

/**
 * Show error message
 */
function showError(message) {
    const tbody = document.getElementById('stations-tbody');
    tbody.innerHTML = `
        <tr class="loading-row">
            <td colspan="10" class="loading-cell error-cell">
                ⚠️ ${escapeHtml(message)}
            </td>
        </tr>
    `;
}

// Initialize when DOM loaded
document.addEventListener('DOMContentLoaded', init);
