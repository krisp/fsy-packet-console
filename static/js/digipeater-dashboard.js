/**
 * Digipeater Dashboard - Main controller for digipeater statistics dashboard
 */

import { APRSApi } from './api.js';
import {
    createHourlyActivityChart,
    createTopStationsChart,
    createPathUsageChart,
    renderActivityHeatmap,
    incrementChartData
} from './digipeater-charts.js';

/**
 * DigipeaterDashboard - Manages the digipeater statistics dashboard
 */
export class DigipeaterDashboard {
    constructor() {
        // API client
        this.api = new APRSApi();

        // Chart instances
        this.charts = {
            hourly: null,
            topStations: null,
            pathUsage: null,
            heatmap: null
        };

        // Current data
        this.data = {
            stats: null,
            activity: null,
            topStations: null,
            pathUsage: null,
            heatmap: null
        };

        // SSE connection
        this.eventSource = null;

        // Update throttling
        this.pendingUpdates = [];
        this.updateTimer = null;

        // Refresh timer
        this.refreshTimer = null;

        // Current time range
        this.currentRange = '24h';

        // Activity feed items
        this.activityFeedItems = [];
        this.maxFeedItems = 50;
    }

    /**
     * Initialize dashboard
     */
    async init() {
        console.log('Initializing digipeater dashboard...');

        try {
            // Load initial data
            await this.loadDashboardData();

            // Create charts
            this.createCharts();

            // Setup real-time updates
            this.setupSSEListener();

            // Setup periodic refresh
            this.refreshTimer = setInterval(() => this.refreshDashboard(), 60000);

            // Setup time range filter handlers
            this.setupTimeRangeHandlers();

            // Setup export handlers
            this.setupExportHandlers();

            console.log('Digipeater dashboard initialized successfully');
        } catch (error) {
            console.error('Failed to initialize digipeater dashboard:', error);
            this.showError('Failed to load dashboard data');
        }
    }

    /**
     * Load all dashboard data from API
     */
    async loadDashboardData() {
        console.log(`Loading dashboard data for range: ${this.currentRange}`);

        try {
            // Fetch all endpoints in parallel
            const [stats, activity, topStations, pathUsage, heatmap] = await Promise.all([
                this.fetchDigipeaterStats(this.currentRange),
                this.fetchDigipeaterActivity(this.currentRange),
                this.fetchDigipeaterTopStations(this.currentRange),
                this.fetchDigipeaterPathUsage(this.currentRange),
                this.fetchDigipeaterHeatmap(this.currentRange)
            ]);

            // Store data
            this.data.stats = stats;
            this.data.activity = activity;
            this.data.topStations = topStations;
            this.data.pathUsage = pathUsage;
            this.data.heatmap = heatmap;

            // Update UI
            this.updateStatsCards(stats);

            console.log('Dashboard data loaded successfully');
        } catch (error) {
            console.error('Failed to load dashboard data:', error);
            throw error;
        }
    }

    /**
     * Fetch digipeater stats from API
     */
    async fetchDigipeaterStats(range) {
        try {
            const response = await fetch(`/api/digipeater/stats?range=${range}`);
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }
            return await response.json();
        } catch (error) {
            console.error('Failed to fetch digipeater stats:', error);
            return { total_packets: 0, unique_stations: 0, peak_rate: 0, uptime: 0 };
        }
    }

    /**
     * Fetch digipeater activity from API
     */
    async fetchDigipeaterActivity(range) {
        try {
            const response = await fetch(`/api/digipeater/activity?range=${range}`);
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }
            return await response.json();
        } catch (error) {
            console.error('Failed to fetch digipeater activity:', error);
            return { buckets: [] };
        }
    }

    /**
     * Fetch top stations from API
     */
    async fetchDigipeaterTopStations(range) {
        try {
            const response = await fetch(`/api/digipeater/top-stations?range=${range}&limit=10`);
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }
            return await response.json();
        } catch (error) {
            console.error('Failed to fetch top stations:', error);
            return { stations: [] };
        }
    }

    /**
     * Fetch path usage from API
     */
    async fetchDigipeaterPathUsage(range) {
        try {
            const response = await fetch(`/api/digipeater/path-usage?range=${range}`);
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }
            return await response.json();
        } catch (error) {
            console.error('Failed to fetch path usage:', error);
            return { path_types: {} };
        }
    }

    /**
     * Fetch heatmap data from API
     */
    async fetchDigipeaterHeatmap(range) {
        try {
            const rangeParam = range.endsWith('h') ? `${parseInt(range) / 24}d` : range;
            const response = await fetch(`/api/digipeater/heatmap?range=${rangeParam}`);
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }
            return await response.json();
        } catch (error) {
            console.error('Failed to fetch heatmap data:', error);
            return { grid: [] };
        }
    }

    /**
     * Create all charts
     */
    createCharts() {
        console.log('Creating charts...');

        // Hourly activity chart
        if (this.data.activity && this.data.activity.buckets) {
            this.charts.hourly = createHourlyActivityChart(
                'hourlyActivityChart',
                this.data.activity.buckets
            );
        }

        // Top stations chart
        if (this.data.topStations && this.data.topStations.stations) {
            this.charts.topStations = createTopStationsChart(
                'topStationsChart',
                this.data.topStations.stations,
                (callsign) => this.handleStationClick(callsign)
            );
        }

        // Path usage chart
        if (this.data.pathUsage && this.data.pathUsage.path_types) {
            this.charts.pathUsage = createPathUsageChart(
                'pathUsageChart',
                this.data.pathUsage.path_types
            );
        }

        // Activity heatmap
        if (this.data.heatmap && this.data.heatmap.grid) {
            this.charts.heatmap = renderActivityHeatmap(
                'activityHeatmap',
                this.data.heatmap.grid
            );
        }

        console.log('Charts created successfully');

        // Populate the top digipeaters table
        this.updateTopDigipeatersTable();
    }

    /**
     * Update the top digipeaters details table
     */
    updateTopDigipeatersTable() {
        const tbody = document.getElementById('digipeaters-tbody');
        if (!tbody || !this.data.topStations || !this.data.topStations.stations) {
            return;
        }

        const stations = this.data.topStations.stations;
        if (stations.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" class="loading-cell">No digipeater activity yet</td></tr>';
            return;
        }

        // Calculate total packets for percentage
        const totalPackets = stations.reduce((sum, s) => sum + s.count, 0);

        // Build table rows
        let html = '';
        stations.forEach((station, index) => {
            const rank = index + 1;
            const percentage = ((station.count / totalPackets) * 100).toFixed(1);
            const lastHeard = new Date(station.last_heard).toLocaleString();
            const frequency = 'N/A'; // Placeholder - could be looked up from station data

            html += `
                <tr>
                    <td>${rank}</td>
                    <td class="callsign-cell">${station.callsign}</td>
                    <td>${station.count}</td>
                    <td>${percentage}%</td>
                    <td>${lastHeard}</td>
                    <td>${frequency}</td>
                </tr>
            `;
        });

        tbody.innerHTML = html;
    }

    /**
     * Update stats cards with animation
     */
    updateStatsCards(stats) {
        if (!stats) return;

        // Total packets
        this.updateStatCard('totalPackets', stats.total_packets || 0);

        // Peak rate
        this.updateStatCard('peakRate', stats.peak_rate || 0, '/hr');

        // Active stations
        this.updateStatCard('activeStations', stats.unique_stations || 0);

        // Uptime
        const uptime = this.formatUptime(stats.uptime || 0);
        const uptimeEl = document.getElementById('uptime');
        if (uptimeEl) {
            uptimeEl.textContent = uptime;
        }
    }

    /**
     * Update individual stat card with animation
     */
    updateStatCard(elementId, value, suffix = '') {
        const element = document.getElementById(elementId);
        if (!element) return;

        const currentValue = parseInt(element.textContent) || 0;

        if (value !== currentValue) {
            // Add animation class
            element.classList.add('stat-updated');

            // Animate counter
            this.animateCounter(element, currentValue, value, suffix);

            // Remove animation class after animation completes
            setTimeout(() => element.classList.remove('stat-updated'), 500);
        }
    }

    /**
     * Animate counter from current to target value
     */
    animateCounter(element, start, end, suffix = '') {
        const duration = 500;
        const steps = 20;
        const increment = (end - start) / steps;
        let current = start;
        let step = 0;

        const timer = setInterval(() => {
            step++;
            current += increment;

            if (step >= steps) {
                element.textContent = Math.round(end) + suffix;
                clearInterval(timer);
            } else {
                element.textContent = Math.round(current) + suffix;
            }
        }, duration / steps);
    }

    /**
     * Format uptime in human-readable format
     */
    formatUptime(seconds) {
        if (seconds < 60) {
            return `${seconds}s`;
        } else if (seconds < 3600) {
            return `${Math.floor(seconds / 60)}m`;
        } else if (seconds < 86400) {
            const hours = Math.floor(seconds / 3600);
            const minutes = Math.floor((seconds % 3600) / 60);
            return `${hours}h ${minutes}m`;
        } else {
            const days = Math.floor(seconds / 86400);
            const hours = Math.floor((seconds % 86400) / 3600);
            return `${days}d ${hours}h`;
        }
    }

    /**
     * Setup SSE listener for real-time updates
     */
    setupSSEListener() {
        console.log('Setting up SSE listener...');

        this.eventSource = this.api.connectSSE(
            (eventType, data) => this.handleSSEEvent(eventType, data),
            (status) => this.handleConnectionChange(status)
        );
    }

    /**
     * Handle SSE event
     */
    handleSSEEvent(eventType, data) {
        switch (eventType) {
            case 'station_update':
                this.handleStationUpdate(data);
                break;

            case 'digipeater_update':
                this.handleDigipeaterUpdate(data);
                break;

            case 'error':
                console.error('SSE error:', data);
                break;
        }
    }

    /**
     * Handle connection status change
     */
    handleConnectionChange(status) {
        console.log(`SSE connection status: ${status}`);

        const statusEl = document.getElementById('connectionStatus');
        if (statusEl) {
            statusEl.textContent = status === 'connected' ? 'Live' : 'Disconnected';
            statusEl.className = status === 'connected' ? 'status-live' : 'status-offline';
        }
    }

    /**
     * Handle station update event
     */
    handleStationUpdate(station) {
        // Check if station was digipeated by us
        if (station.digipeated) {
            // Queue update for throttled processing
            this.pendingUpdates.push({
                type: 'station',
                data: station,
                timestamp: new Date()
            });

            // Schedule throttled update
            this.scheduleThrottledUpdate();
        }
    }

    /**
     * Handle digipeater update event
     */
    handleDigipeaterUpdate(update) {
        // Queue update
        this.pendingUpdates.push({
            type: 'digipeater',
            data: update,
            timestamp: new Date()
        });

        // Add to live feed
        this.addToLiveFeed(update);

        // Schedule throttled update
        this.scheduleThrottledUpdate();
    }

    /**
     * Schedule throttled update
     */
    scheduleThrottledUpdate() {
        if (this.updateTimer) {
            clearTimeout(this.updateTimer);
        }

        this.updateTimer = setTimeout(() => {
            this.processThrottledUpdates();
        }, 1000);
    }

    /**
     * Process pending updates
     */
    processThrottledUpdates() {
        if (this.pendingUpdates.length === 0) return;

        console.log(`Processing ${this.pendingUpdates.length} pending updates`);

        // Aggregate updates
        const updates = {
            totalPackets: 0,
            stations: new Set(),
            pathTypes: {}
        };

        this.pendingUpdates.forEach(update => {
            if (update.type === 'digipeater') {
                updates.totalPackets++;
                updates.stations.add(update.data.station_call);

                const pathType = update.data.path_type || 'Other';
                updates.pathTypes[pathType] = (updates.pathTypes[pathType] || 0) + 1;
            }
        });

        // Update stats cards
        if (this.data.stats) {
            this.data.stats.total_packets += updates.totalPackets;
            this.data.stats.unique_stations = Math.max(
                this.data.stats.unique_stations,
                updates.stations.size
            );

            this.updateStatsCards(this.data.stats);
        }

        // Increment chart data
        this.incrementChartData(updates);

        // Clear pending updates
        this.pendingUpdates = [];
        this.updateTimer = null;
    }

    /**
     * Add update to live activity feed
     */
    addToLiveFeed(update) {
        const feedContainer = document.getElementById('activityFeed');
        if (!feedContainer) return;

        // Create feed item element
        const item = document.createElement('div');
        item.className = 'digipeater-activity-item';

        const timestamp = new Date(update.timestamp || Date.now());
        const timeStr = timestamp.toLocaleTimeString();

        // Create elements safely to prevent XSS
        const timeDiv = document.createElement('div');
        timeDiv.className = 'activity-time';
        timeDiv.textContent = timeStr;

        const stationDiv = document.createElement('div');
        stationDiv.className = 'activity-station';
        stationDiv.textContent = update.station_call;

        const pathDiv = document.createElement('div');
        pathDiv.className = 'activity-path';
        pathDiv.textContent = update.path_type || 'Unknown';

        item.appendChild(timeDiv);
        item.appendChild(stationDiv);
        item.appendChild(pathDiv);

        // Prepend to feed
        feedContainer.insertBefore(item, feedContainer.firstChild);

        // Track in memory
        this.activityFeedItems.unshift(update);

        // Limit feed size
        if (this.activityFeedItems.length > this.maxFeedItems) {
            this.activityFeedItems.pop();

            const lastChild = feedContainer.lastChild;
            if (lastChild) {
                feedContainer.removeChild(lastChild);
            }
        }

        // Add entrance animation
        setTimeout(() => item.classList.add('activity-item-visible'), 10);
    }

    /**
     * Increment chart data without full redraw
     */
    incrementChartData(updates) {
        // Update hourly chart (add to current hour bucket)
        if (this.charts.hourly) {
            const now = new Date();
            incrementChartData(this.charts.hourly, {
                timestamp: now.toISOString(),
                value: updates.totalPackets,
                value2: updates.stations.size
            });
        }

        // Update path usage chart
        if (this.charts.pathUsage) {
            Object.entries(updates.pathTypes).forEach(([pathType, count]) => {
                incrementChartData(this.charts.pathUsage, {
                    path_type: pathType,
                    count: count
                });
            });
        }

        // Top stations chart will be updated on next full refresh
    }

    /**
     * Full dashboard refresh
     */
    async refreshDashboard() {
        console.log('Performing full dashboard refresh...');

        try {
            await this.loadDashboardData();

            // Destroy old charts
            Object.values(this.charts).forEach(chart => {
                if (chart && typeof chart.destroy === 'function') {
                    chart.destroy();
                }
            });

            // Recreate charts
            this.createCharts();

            console.log('Dashboard refreshed successfully');
        } catch (error) {
            console.error('Failed to refresh dashboard:', error);
        }
    }

    /**
     * Handle time range change
     */
    async handleTimeRangeChange(range) {
        console.log(`Time range changed to: ${range}`);

        this.currentRange = range;

        // Update active button state
        document.querySelectorAll('.time-range-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.range === range);
        });

        // Reload dashboard with new range
        await this.refreshDashboard();
    }

    /**
     * Setup time range filter handlers
     */
    setupTimeRangeHandlers() {
        const buttons = document.querySelectorAll('.time-range-btn');

        buttons.forEach(btn => {
            btn.addEventListener('click', () => {
                const range = btn.dataset.range;
                if (range) {
                    this.handleTimeRangeChange(range);
                }
            });
        });
    }

    /**
     * Handle station click on chart
     */
    handleStationClick(callsign) {
        console.log(`Station clicked: ${callsign}`);

        // Navigate to station detail page
        window.location.href = `/stations/${encodeURIComponent(callsign)}`;
    }

    /**
     * Export current data to CSV
     */
    exportToCSV() {
        console.log('Exporting data to CSV...');

        try {
            // Build CSV content
            let csv = 'Timestamp,Station,Path Type,Count\n';

            // Add activity data
            if (this.data.activity && this.data.activity.buckets) {
                this.data.activity.buckets.forEach(bucket => {
                    csv += `${bucket.timestamp},All Stations,All Paths,${bucket.packet_count}\n`;
                });
            }

            // Create download
            const blob = new Blob([csv], { type: 'text/csv' });
            const url = URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = url;
            link.download = `digipeater-stats-${new Date().toISOString()}.csv`;
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            URL.revokeObjectURL(url);

            console.log('CSV export complete');
        } catch (error) {
            console.error('Failed to export CSV:', error);
            this.showError('Failed to export CSV');
        }
    }

    /**
     * Export current data to JSON
     */
    exportToJSON() {
        console.log('Exporting data to JSON...');

        try {
            // Build JSON object
            const exportData = {
                timestamp: new Date().toISOString(),
                range: this.currentRange,
                stats: this.data.stats,
                activity: this.data.activity,
                topStations: this.data.topStations,
                pathUsage: this.data.pathUsage,
                heatmap: this.data.heatmap
            };

            // Create download
            const json = JSON.stringify(exportData, null, 2);
            const blob = new Blob([json], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = url;
            link.download = `digipeater-stats-${new Date().toISOString()}.json`;
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            URL.revokeObjectURL(url);

            console.log('JSON export complete');
        } catch (error) {
            console.error('Failed to export JSON:', error);
            this.showError('Failed to export JSON');
        }
    }

    /**
     * Setup export button handlers
     */
    setupExportHandlers() {
        const csvBtn = document.getElementById('exportCSV');
        if (csvBtn) {
            csvBtn.addEventListener('click', () => this.exportToCSV());
        }

        const jsonBtn = document.getElementById('exportJSON');
        if (jsonBtn) {
            jsonBtn.addEventListener('click', () => this.exportToJSON());
        }
    }

    /**
     * Show error message
     */
    showError(message) {
        console.error(message);

        const errorEl = document.getElementById('errorMessage');
        if (errorEl) {
            errorEl.textContent = message;
            errorEl.style.display = 'block';

            setTimeout(() => {
                errorEl.style.display = 'none';
            }, 5000);
        }
    }

    /**
     * Cleanup resources
     */
    destroy() {
        console.log('Destroying digipeater dashboard...');

        // Close SSE connection
        if (this.eventSource) {
            this.eventSource.close();
            this.eventSource = null;
        }

        // Clear timers
        if (this.updateTimer) {
            clearTimeout(this.updateTimer);
            this.updateTimer = null;
        }

        if (this.refreshTimer) {
            clearInterval(this.refreshTimer);
            this.refreshTimer = null;
        }

        // Destroy charts
        Object.values(this.charts).forEach(chart => {
            if (chart && typeof chart.destroy === 'function') {
                chart.destroy();
            }
        });

        console.log('Digipeater dashboard destroyed');
    }
}

// Initialize dashboard when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    const dashboard = new DigipeaterDashboard();
    dashboard.init();

    // Store instance globally for debugging
    window.digipeaterDashboard = dashboard;
});
