/**
 * Weather Map - Surface Station Model Visualization
 *
 * Displays APRS weather stations using standard meteorological surface station models
 * with wind barbs, temperature/dew point displays, and pressure information.
 */

import { createBaseLayers, SSEManager } from './utils.js';

/**
 * StationModelRenderer - Handles canvas drawing of meteorological station models
 */
class StationModelRenderer {
    constructor(ctx) {
        this.ctx = ctx;
    }

    /**
     * Convert Fahrenheit to Celsius
     */
    fahrenheitToCelsius(tempF) {
        return (tempF - 32) * 5.0 / 9.0;
    }

    /**
     * Convert MPH to knots (always for wind barbs)
     */
    mphToKnots(mph) {
        return mph * 0.868976;
    }

    /**
     * Format pressure - last 3 digits of mbar
     */
    formatPressure(pressureMbar) {
        if (!pressureMbar) return null;

        // Traditional aviation format: round to whole mbar, show last 3 digits
        // E.g., 1013.6 → 1014 → "014"
        const pressureRounded = Math.round(pressureMbar); // 1014
        const last3 = pressureRounded % 1000; // 14
        return last3.toString().padStart(3, '0'); // "014"
    }

    /**
     * Format temperature with unit
     */
    formatTemperature(tempF, useMetric) {
        if (tempF === null || tempF === undefined) return null;

        if (useMetric) {
            const tempC = this.fahrenheitToCelsius(tempF);
            return Math.round(tempC).toString();
        } else {
            return Math.round(tempF).toString();
        }
    }

    /**
     * Get pressure tendency symbol
     */
    getPressureTendencySymbol(tendency) {
        if (tendency === 'rising') return '⬀';
        if (tendency === 'falling') return '⬂';
        if (tendency === 'steady') return '—';
        return '';
    }

    /**
     * Draw a line
     */
    drawLine(x1, y1, x2, y2, color = '#003399', width = 1.5) {
        this.ctx.strokeStyle = color;
        this.ctx.lineWidth = width;
        this.ctx.beginPath();
        this.ctx.moveTo(x1, y1);
        this.ctx.lineTo(x2, y2);
        this.ctx.stroke();
    }

    /**
     * Draw a circle
     */
    drawCircle(x, y, radius, fill = false, color = '#003399', width = 1.5) {
        this.ctx.strokeStyle = color;
        this.ctx.fillStyle = color;
        this.ctx.lineWidth = width;
        this.ctx.beginPath();
        this.ctx.arc(x, y, radius, 0, 2 * Math.PI);
        if (fill) {
            this.ctx.fill();
        } else {
            this.ctx.stroke();
        }
    }

    /**
     * Draw text
     */
    drawText(text, x, y, options = {}) {
        const {
            color = '#003399',
            font = '12px monospace',
            align = 'left',
            baseline = 'top',
            background = false
        } = options;

        this.ctx.fillStyle = color;
        this.ctx.font = font;
        this.ctx.textAlign = align;
        this.ctx.textBaseline = baseline;

        // Optional background
        if (background) {
            const metrics = this.ctx.measureText(text);
            const bgPadding = 2;
            const bgX = align === 'right' ? x - metrics.width - bgPadding :
                       align === 'center' ? x - metrics.width / 2 - bgPadding :
                       x - bgPadding;
            const bgY = baseline === 'bottom' ? y - 12 - bgPadding :
                       baseline === 'middle' ? y - 6 - bgPadding :
                       y - bgPadding;

            this.ctx.fillStyle = 'rgba(0, 0, 0, 0.7)';
            this.ctx.fillRect(bgX, bgY, metrics.width + bgPadding * 2, 14 + bgPadding * 2);
            this.ctx.fillStyle = color;
        }

        this.ctx.fillText(text, x, y);
    }

    /**
     * Draw station circle (center dot)
     */
    drawStationCircle(x, y, color = '#003399') {
        this.drawCircle(x, y, 4, false, color);
    }

    /**
     * Draw wind barb
     *
     * Aviation standard:
     * - Barbs point INTO wind direction
     * - Pennant = 50kt, Full barb = 10kt, Half barb = 5kt
     * - Barbs on right side of shaft (relative to direction)
     * - Calm wind (<5kt) = circle around station
     */
    drawWindBarb(x, y, windSpeed, windDirection, color = '#003399') {
        if (windSpeed === null || windSpeed === undefined ||
            windDirection === null || windDirection === undefined) {
            return;
        }

        const speedKnots = this.mphToKnots(windSpeed);

        // Calm wind - draw circle
        if (speedKnots < 5) {
            this.drawCircle(x, y, 8, false, color, 1.5);
            return;
        }

        // Wind barb shaft length
        const shaftLength = 40;

        // Convert direction to radians (wind direction is where it's FROM)
        // Canvas y-axis is inverted, so we adjust
        const directionRad = (windDirection - 90) * Math.PI / 180;

        // Shaft endpoint (pointing FROM wind direction, i.e., TO wind direction)
        const shaftEndX = x + shaftLength * Math.cos(directionRad);
        const shaftEndY = y + shaftLength * Math.sin(directionRad);

        // Draw shaft
        this.drawLine(x, y, shaftEndX, shaftEndY, color, 2);

        // Calculate barbs (pennants, full barbs, half barbs)
        let remainingSpeed = Math.round(speedKnots);

        // Count pennants (50kt each)
        const pennants = Math.floor(remainingSpeed / 50);
        remainingSpeed %= 50;

        // Count full barbs (10kt each)
        const fullBarbs = Math.floor(remainingSpeed / 10);
        remainingSpeed %= 10;

        // Count half barbs (5kt)
        const halfBarbs = remainingSpeed >= 5 ? 1 : 0;

        // Draw barbs starting from shaft end, moving toward station
        // Perpendicular vector (to the right of the shaft)
        const perpX = -Math.sin(directionRad);
        const perpY = Math.cos(directionRad);

        let currentDist = shaftLength - 5; // Start 5px from end
        const barbSpacing = 7; // Space between barbs
        const barbLength = 12; // Length of barb

        // Draw pennants
        for (let i = 0; i < pennants; i++) {
            const baseX = x + currentDist * Math.cos(directionRad);
            const baseY = y + currentDist * Math.sin(directionRad);

            // Pennant is a filled triangle
            const tip1X = baseX + perpX * barbLength;
            const tip1Y = baseY + perpY * barbLength;
            const base2X = baseX - barbSpacing * Math.cos(directionRad);
            const base2Y = baseY - barbSpacing * Math.sin(directionRad);

            this.ctx.fillStyle = color;
            this.ctx.beginPath();
            this.ctx.moveTo(baseX, baseY);
            this.ctx.lineTo(tip1X, tip1Y);
            this.ctx.lineTo(base2X, base2Y);
            this.ctx.closePath();
            this.ctx.fill();

            currentDist -= barbSpacing + 3; // Extra space after pennant
        }

        // Draw full barbs
        for (let i = 0; i < fullBarbs; i++) {
            const baseX = x + currentDist * Math.cos(directionRad);
            const baseY = y + currentDist * Math.sin(directionRad);
            const tipX = baseX + perpX * barbLength;
            const tipY = baseY + perpY * barbLength;

            this.drawLine(baseX, baseY, tipX, tipY, color, 2);
            currentDist -= barbSpacing;
        }

        // Draw half barb
        if (halfBarbs > 0) {
            const baseX = x + currentDist * Math.cos(directionRad);
            const baseY = y + currentDist * Math.sin(directionRad);
            const tipX = baseX + perpX * (barbLength / 2);
            const tipY = baseY + perpY * (barbLength / 2);

            this.drawLine(baseX, baseY, tipX, tipY, color, 2);
        }
    }

    /**
     * Draw complete station model
     */
    drawStationModel(x, y, weather, options = {}) {
        const {
            showTemp = true,
            showDewpoint = true,
            showWind = true,
            showPressure = true,
            showLabel = true,
            useMetric = false,
            color = '#003399'
        } = options;

        // Use white text on dark backgrounds, dark blue for graphics
        const textColor = '#ffffff';
        const graphicsColor = color;

        // Draw station center
        this.drawStationCircle(x, y, graphicsColor);

        // Draw temperature (upper-left)
        if (showTemp && weather.temperature !== null && weather.temperature !== undefined) {
            const tempStr = this.formatTemperature(weather.temperature, useMetric);
            if (tempStr) {
                this.drawText(tempStr, x - 8, y - 15, {
                    color: textColor,
                    font: 'bold 13px monospace',
                    align: 'right',
                    baseline: 'bottom',
                    background: true
                });
            }
        }

        // Draw dew point (lower-left)
        if (showDewpoint && weather.dew_point !== null && weather.dew_point !== undefined) {
            const dewStr = this.formatTemperature(weather.dew_point, useMetric);
            if (dewStr) {
                this.drawText(dewStr, x - 8, y + 5, {
                    color: textColor,
                    font: '12px monospace',
                    align: 'right',
                    baseline: 'top',
                    background: true
                });
            }
        }

        // Draw pressure (upper-right)
        if (showPressure && weather.pressure !== null && weather.pressure !== undefined) {
            const pressureStr = this.formatPressure(weather.pressure);
            const tendencySymbol = this.getPressureTendencySymbol(weather.pressure_tendency);
            const fullPressureStr = pressureStr + tendencySymbol;

            this.drawText(fullPressureStr, x + 8, y - 15, {
                color: textColor,
                font: '12px monospace',
                align: 'left',
                baseline: 'bottom',
                background: true
            });
        }

        // Draw wind barb
        if (showWind) {
            this.drawWindBarb(x, y, weather.wind_speed, weather.wind_direction, graphicsColor);
        }

        // Draw callsign label (below station)
        if (showLabel && weather.station) {
            this.drawText(weather.station, x, y + 20, {
                color: textColor,
                font: '11px monospace',
                align: 'center',
                baseline: 'top',
                background: true
            });
        }
    }
}

/**
 * WeatherMapApp - Main application controller
 */
class WeatherMapApp {
    constructor() {
        this.map = null;
        this.canvasLayer = null;
        this.canvas = null;
        this.ctx = null;
        this.renderer = null;
        this.weatherStations = [];
        this.eventSource = null;
        this.hasGPSLocation = false; // Track if we have GPS

        this.displayOptions = {
            use_metric: false,
            show_labels: true,
            show_temp: true,
            show_dewpoint: true,
            show_wind: true,
            show_pressure: true
        };
    }

    /**
     * Initialize the application
     */
    async init() {
        await this.initMap();
        this.setupControls();
        await this.loadWeatherStations();
        this.setupSSE();
    }

    /**
     * Initialize Leaflet map
     */
    async initMap() {
        // Fetch GPS location to center map on user's station
        let initialCenter = [42.4, -71.1]; // Default: Boston area
        let initialZoom = 8;

        try {
            const response = await fetch('/api/gps');
            if (response.ok) {
                const gpsData = await response.json();
                if (gpsData.latitude && gpsData.longitude) {
                    initialCenter = [gpsData.latitude, gpsData.longitude];
                    initialZoom = 10; // Closer zoom when we have GPS
                    this.hasGPSLocation = true;
                }
            }
        } catch (error) {
            // GPS not available - use default center
        }

        // Create map
        this.map = L.map('weather-map', {
            center: initialCenter,
            zoom: initialZoom,
            zoomControl: true
        });

        // Base tile layers from shared utils
        const { layers: baseLayers } = createBaseLayers();

        // Add default layer (Terrain)
        baseLayers['Terrain'].addTo(this.map);

        // Add layer control
        L.control.layers(baseLayers, {}, {
            position: 'topleft',
            collapsed: false
        }).addTo(this.map);

        // Create custom canvas overlay
        this.createCanvasOverlay();

        // Redraw on map events (including during movement for smooth updates)
        this.map.on('move', () => this.redraw());
        this.map.on('zoom', () => this.redraw());
        this.map.on('moveend', () => this.redraw());
        this.map.on('zoomend', () => this.redraw());
        this.map.on('resize', () => this.resizeCanvas());
    }

    /**
     * Create canvas overlay layer
     */
    createCanvasOverlay() {
        const self = this;
        const CanvasLayer = L.Layer.extend({
            onAdd: function(map) {
                const size = map.getSize();
                this._canvas = L.DomUtil.create('canvas', 'weather-canvas-overlay');
                this._canvas.width = size.x;
                this._canvas.height = size.y;
                this._canvas.style.position = 'absolute';
                this._canvas.style.cursor = 'pointer';

                // Get the overlay pane
                const pane = map.getPanes().overlayPane;
                pane.appendChild(this._canvas);

                this._map = map;

                // Position canvas at map's top-left (in layer coordinates)
                this._reset();

                // Update position on viewreset and move
                map.on('viewreset', this._reset, this);
                map.on('move', this._reset, this);
                map.on('zoom', this._reset, this);

                // Add click handler
                L.DomEvent.on(this._canvas, 'click', self.handleCanvasClick.bind(self));
            },

            onRemove: function(map) {
                map.off('viewreset', this._reset, this);
                map.off('move', this._reset, this);
                map.off('zoom', this._reset, this);
                L.DomEvent.off(this._canvas, 'click');
                L.DomUtil.remove(this._canvas);
            },

            _reset: function() {
                // Position the canvas at the top-left corner of the map in layer coordinates
                const topLeft = this._map.containerPointToLayerPoint([0, 0]);
                L.DomUtil.setPosition(this._canvas, topLeft);
            },

            getCanvas: function() {
                return this._canvas;
            }
        });

        this.canvasLayer = new CanvasLayer();
        this.canvasLayer.addTo(this.map);
        this.canvas = this.canvasLayer.getCanvas();
        this.ctx = this.canvas.getContext('2d');
        this.renderer = new StationModelRenderer(this.ctx);
    }

    /**
     * Resize canvas to match map
     */
    resizeCanvas() {
        if (!this.canvas || !this.map) return;

        const size = this.map.getSize();
        this.canvas.width = size.x;
        this.canvas.height = size.y;
        this.redraw();
    }

    /**
     * Setup control panel event handlers
     */
    setupControls() {
        const checkboxIds = ['use-metric', 'show-labels', 'show-temp', 'show-dewpoint', 'show-wind', 'show-pressure'];

        checkboxIds.forEach(id => {
            const checkbox = document.getElementById(id);
            if (checkbox) {
                const optionKey = id.replace(/-/g, '_');
                checkbox.addEventListener('change', (e) => {
                    this.displayOptions[optionKey] = e.target.checked;

                    // Update legend if metric toggle changed
                    if (id === 'use-metric') {
                        this.updateLegend();
                    }

                    this.redraw();
                });
            }
        });
    }

    /**
     * Update legend to show current units
     */
    updateLegend() {
        const unit = this.displayOptions.use_metric ? '°C' : '°F';
        const tempUnit = document.getElementById('legend-temp-unit');
        const dewpointUnit = document.getElementById('legend-dewpoint-unit');

        if (tempUnit) tempUnit.textContent = unit;
        if (dewpointUnit) dewpointUnit.textContent = unit;
    }

    /**
     * Handle canvas click to show station popup
     */
    handleCanvasClick(event) {
        if (!this.canvas || !this.map) return;

        // Get click position relative to canvas
        const rect = this.canvas.getBoundingClientRect();
        const clickX = event.clientX - rect.left;
        const clickY = event.clientY - rect.top;

        // Get the canvas offset in layer coordinates
        const canvasLayerPoint = this.map.containerPointToLayerPoint([0, 0]);

        // Find station within click radius (30px for easier clicking)
        const clickRadius = 30;
        let closestStation = null;
        let closestDistance = clickRadius;

        for (const station of this.weatherStations) {
            // Convert station position to canvas coordinates (same as in redraw)
            const stationLayerPoint = this.map.latLngToLayerPoint([station.latitude, station.longitude]);
            const canvasX = stationLayerPoint.x - canvasLayerPoint.x;
            const canvasY = stationLayerPoint.y - canvasLayerPoint.y;

            const distance = Math.sqrt(
                Math.pow(canvasX - clickX, 2) + Math.pow(canvasY - clickY, 2)
            );

            if (distance < closestDistance) {
                closestDistance = distance;
                closestStation = station;
            }
        }

        if (closestStation) {
            this.showStationPopup(closestStation);

            // Stop event propagation to prevent map from handling the click
            event.stopPropagation();
            event.preventDefault();
        }
    }

    /**
     * Generate temperature line chart HTML with proper axes
     */
    generateTemperatureChart(history, useMetric, currentTemp) {
        if (!history || history.length === 0) {
            return '<div style="color: #999; font-size: 11px;">No temperature data</div>';
        }

        // Filter and sort history with valid temperatures and timestamps
        const tempData = history
            .filter(h => h.temperature !== null && h.temperature !== undefined && h.timestamp)
            .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp)) // Sort oldest to newest
            .slice(-12); // Last 12 samples (most recent)

        if (tempData.length === 0) {
            return '<div style="color: #999; font-size: 11px;">No temperature data</div>';
        }

        // Chart dimensions with margins for axes
        const totalWidth = 200;
        const totalHeight = 120;
        const marginLeft = 35;
        const marginRight = 10;
        const marginTop = 10;
        const marginBottom = 25;
        const plotWidth = totalWidth - marginLeft - marginRight;
        const plotHeight = totalHeight - marginTop - marginBottom;

        // Convert temperatures to desired unit
        const temps = tempData.map(h => useMetric
            ? this.renderer.fahrenheitToCelsius(h.temperature)
            : h.temperature
        );

        // Include current temperature in range calculation
        const displayCurrentTemp = (currentTemp !== null && currentTemp !== undefined)
            ? (useMetric ? this.renderer.fahrenheitToCelsius(currentTemp) : currentTemp)
            : null;

        // Calculate temperature range with padding (include current temp)
        const allTemps = displayCurrentTemp !== null ? [...temps, displayCurrentTemp] : temps;
        const minTemp = Math.min(...allTemps);
        const maxTemp = Math.max(...allTemps);
        const tempPadding = (maxTemp - minTemp) * 0.1 || 1;
        const yMin = Math.floor(minTemp - tempPadding);
        const yMax = Math.ceil(maxTemp + tempPadding);
        const yRange = yMax - yMin;

        // Start SVG
        let svg = `<svg width="${totalWidth}" height="${totalHeight}" style="font-family: Arial, sans-serif; font-size: 9px;">`;

        // Draw background grid
        svg += `<rect x="${marginLeft}" y="${marginTop}" width="${plotWidth}" height="${plotHeight}" fill="#f8f8f8" stroke="#ddd"/>`;

        // Draw horizontal grid lines (temperature)
        const numYTicks = 5;
        for (let i = 0; i <= numYTicks; i++) {
            const temp = yMin + (yRange * i / numYTicks);
            const y = marginTop + plotHeight - (plotHeight * i / numYTicks);

            // Grid line
            svg += `<line x1="${marginLeft}" y1="${y}" x2="${marginLeft + plotWidth}" y2="${y}" stroke="#ddd" stroke-width="0.5"/>`;

            // Y-axis label
            svg += `<text x="${marginLeft - 5}" y="${y + 3}" text-anchor="end" fill="#666">${Math.round(temp)}°</text>`;
        }

        // Draw Y-axis
        svg += `<line x1="${marginLeft}" y1="${marginTop}" x2="${marginLeft}" y2="${marginTop + plotHeight}" stroke="#333" stroke-width="1"/>`;

        // Draw X-axis
        svg += `<line x1="${marginLeft}" y1="${marginTop + plotHeight}" x2="${marginLeft + plotWidth}" y2="${marginTop + plotHeight}" stroke="#333" stroke-width="1"/>`;

        // Build line points
        const points = temps.map((temp, i) => {
            const x = marginLeft + (plotWidth * i / (temps.length - 1 || 1));
            const y = marginTop + plotHeight - ((temp - yMin) / yRange) * plotHeight;
            return `${x},${y}`;
        }).join(' ');

        // Draw line
        svg += `<polyline points="${points}" fill="none" stroke="#0066cc" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`;

        // Draw points
        temps.forEach((temp, i) => {
            const x = marginLeft + (plotWidth * i / (temps.length - 1 || 1));
            const y = marginTop + plotHeight - ((temp - yMin) / yRange) * plotHeight;

            // Point circle
            svg += `<circle cx="${x}" cy="${y}" r="3" fill="#0066cc" stroke="white" stroke-width="1"/>`;
        });

        // Show current temperature indicator (if provided)
        if (displayCurrentTemp !== null) {
            // Position current temp at the far right edge
            const currentX = marginLeft + plotWidth;
            const currentY = marginTop + plotHeight - ((displayCurrentTemp - yMin) / yRange) * plotHeight;

            // Draw connecting dashed line from last history point to current
            if (temps.length > 0) {
                const lastIdx = temps.length - 1;
                const lastX = marginLeft + (plotWidth * lastIdx / Math.max(1, temps.length - 1));
                const lastY = marginTop + plotHeight - ((temps[lastIdx] - yMin) / yRange) * plotHeight;

                // Only draw if current temp is different from last history temp
                if (Math.abs(displayCurrentTemp - temps[lastIdx]) > 0.5) {
                    svg += `<line x1="${lastX}" y1="${lastY}" x2="${currentX}" y2="${currentY}" stroke="#ff6600" stroke-width="2" stroke-dasharray="4,3" opacity="0.7"/>`;
                }
            }

            // Highlight circle (current temperature)
            svg += `<circle cx="${currentX}" cy="${currentY}" r="5" fill="#ff6600" stroke="white" stroke-width="2"/>`;

            // Label with current value
            svg += `<text x="${currentX}" y="${currentY - 10}" text-anchor="end" fill="#ff6600" font-weight="bold" font-size="11px">${Math.round(displayCurrentTemp)}°</text>`;
        }

        // X-axis labels (time)
        const numXTicks = Math.min(4, tempData.length);
        for (let i = 0; i < numXTicks; i++) {
            const idx = Math.floor((tempData.length - 1) * i / (numXTicks - 1 || 1));
            const timestamp = tempData[idx].timestamp;
            const date = new Date(timestamp);
            const timeStr = date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });

            const x = marginLeft + (plotWidth * idx / (temps.length - 1 || 1));
            svg += `<text x="${x}" y="${marginTop + plotHeight + 15}" text-anchor="middle" fill="#666" font-size="8px">${timeStr}</text>`;
        }

        // Y-axis label
        const unit = useMetric ? '°C' : '°F';
        svg += `<text x="${marginLeft / 2}" y="${marginTop + plotHeight / 2}" text-anchor="middle" fill="#333" font-weight="bold" transform="rotate(-90 ${marginLeft / 2} ${marginTop + plotHeight / 2})">${unit}</text>`;

        svg += '</svg>';
        return svg;
    }

    /**
     * Generate professional meteorological wind rose
     */
    generateWindRose(history) {
        if (!history || history.length === 0) {
            return '<div style="color: #999; font-size: 11px;">No wind data</div>';
        }

        // Filter history with valid wind data
        const windData = history.filter(h =>
            h.wind_direction !== null && h.wind_direction !== undefined &&
            h.wind_speed !== null && h.wind_speed !== undefined
        );

        if (windData.length === 0) {
            return '<div style="color: #999; font-size: 11px;">No wind data</div>';
        }

        // Define speed ranges (mph) and colors
        const speedRanges = [
            { min: 0, max: 5, color: '#d4f1f4', label: '0-5' },
            { min: 5, max: 10, color: '#75e6da', label: '5-10' },
            { min: 10, max: 15, color: '#189ab4', label: '10-15' },
            { min: 15, max: 25, color: '#05445e', label: '15-25' },
            { min: 25, max: 999, color: '#1a1a2e', label: '25+' }
        ];

        // 8 directional sectors
        const dirNames = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
        const numDirections = 8;

        // Initialize data structure: direction -> speed range -> count
        const sectors = {};
        dirNames.forEach(dir => {
            sectors[dir] = speedRanges.map(() => 0);
        });

        // Categorize each wind sample
        windData.forEach(w => {
            const dirIndex = Math.round(w.wind_direction / 45) % 8;
            const dirName = dirNames[dirIndex];

            // Find which speed range this belongs to
            const rangeIndex = speedRanges.findIndex(r => w.wind_speed >= r.min && w.wind_speed < r.max);
            if (rangeIndex >= 0) {
                sectors[dirName][rangeIndex]++;
            }
        });

        // Calculate maximum frequency for scaling
        let maxFrequency = 0;
        dirNames.forEach(dir => {
            const totalForDir = sectors[dir].reduce((sum, count) => sum + count, 0);
            if (totalForDir > maxFrequency) maxFrequency = totalForDir;
        });

        if (maxFrequency === 0) {
            return '<div style="color: #999; font-size: 11px;">No wind data</div>';
        }

        // Chart dimensions
        const size = 180;
        const center = size / 2;
        const maxRadius = 65;
        const innerRadius = 5;

        let svg = `<svg width="${size}" height="${size + 40}" style="font-family: Arial, sans-serif; font-size: 8px;">`;

        // Draw concentric circles (frequency rings)
        const numRings = 4;
        for (let i = 1; i <= numRings; i++) {
            const radius = innerRadius + (maxRadius - innerRadius) * (i / numRings);
            svg += `<circle cx="${center}" cy="${center}" r="${radius}" fill="none" stroke="#ddd" stroke-width="0.5"/>`;

            // Label showing percentage
            const pct = Math.round((i / numRings) * 100);
            svg += `<text x="${center + radius + 2}" y="${center - 2}" fill="#999" font-size="7px">${pct}%</text>`;
        }

        // Draw radial lines for each direction
        dirNames.forEach((dir, i) => {
            const angle = (i * 45 - 90) * Math.PI / 180;
            const x = center + maxRadius * Math.cos(angle);
            const y = center + maxRadius * Math.sin(angle);
            svg += `<line x1="${center}" y1="${center}" x2="${x}" y2="${y}" stroke="#ddd" stroke-width="0.5"/>`;
        });

        // Draw wedges for each direction
        dirNames.forEach((dir, dirIndex) => {
            const baseAngle = (dirIndex * 45 - 90) * Math.PI / 180;
            const wedgeWidth = (45 * Math.PI / 180); // 45 degrees in radians

            // Calculate cumulative frequencies for stacking
            let cumulativeRadius = innerRadius;

            for (let rangeIndex = 0; rangeIndex < speedRanges.length; rangeIndex++) {
                const count = sectors[dir][rangeIndex];
                if (count === 0) continue;

                // Calculate how much of the max radius this segment should use
                const frequency = count / maxFrequency;
                const segmentHeight = frequency * (maxRadius - innerRadius);
                const outerRadius = cumulativeRadius + segmentHeight;

                // Draw wedge segment as a path
                const startAngle = baseAngle - wedgeWidth / 2;
                const endAngle = baseAngle + wedgeWidth / 2;

                // Calculate arc path
                const x1Inner = center + cumulativeRadius * Math.cos(startAngle);
                const y1Inner = center + cumulativeRadius * Math.sin(startAngle);
                const x2Inner = center + cumulativeRadius * Math.cos(endAngle);
                const y2Inner = center + cumulativeRadius * Math.sin(endAngle);
                const x1Outer = center + outerRadius * Math.cos(startAngle);
                const y1Outer = center + outerRadius * Math.sin(startAngle);
                const x2Outer = center + outerRadius * Math.cos(endAngle);
                const y2Outer = center + outerRadius * Math.sin(endAngle);

                svg += `<path d="M ${x1Inner} ${y1Inner} L ${x1Outer} ${y1Outer} A ${outerRadius} ${outerRadius} 0 0 1 ${x2Outer} ${y2Outer} L ${x2Inner} ${y2Inner} A ${cumulativeRadius} ${cumulativeRadius} 0 0 0 ${x1Inner} ${y1Inner}" fill="${speedRanges[rangeIndex].color}" stroke="white" stroke-width="0.5"/>`;

                cumulativeRadius = outerRadius;
            }
        });

        // Draw direction labels
        dirNames.forEach((dir, i) => {
            const angle = (i * 45 - 90) * Math.PI / 180;
            const labelRadius = maxRadius + 15;
            const x = center + labelRadius * Math.cos(angle);
            const y = center + labelRadius * Math.sin(angle) + 3;

            svg += `<text x="${x}" y="${y}" text-anchor="middle" fill="#333" font-weight="bold" font-size="10px">${dir}</text>`;
        });

        // Draw center circle
        svg += `<circle cx="${center}" cy="${center}" r="${innerRadius}" fill="white" stroke="#333" stroke-width="1"/>`;

        // Draw legend
        const legendY = size + 5;
        const legendX = 10;
        let legendHtml = '';

        speedRanges.forEach((range, i) => {
            const x = legendX + i * 34;
            svg += `<rect x="${x}" y="${legendY}" width="12" height="8" fill="${range.color}" stroke="#999" stroke-width="0.5"/>`;
            svg += `<text x="${x + 14}" y="${legendY + 7}" fill="#333" font-size="7px">${range.label}</text>`;
        });

        svg += `<text x="${center}" y="${legendY + 22}" text-anchor="middle" fill="#666" font-size="7px">Wind Speed (mph)</text>`;

        svg += '</svg>';
        return svg;
    }

    /**
     * Show popup with station details
     */
    async showStationPopup(station) {
        const weather = station.weather;
        const useMetric = this.displayOptions.use_metric;

        // Fetch full station data including weather history
        let weatherHistory = [];
        try {
            const response = await fetch(`/api/stations/${encodeURIComponent(station.callsign)}`);
            if (response.ok) {
                const fullStation = await response.json();
                weatherHistory = fullStation.weather_history || [];
            }
        } catch (error) {
            console.warn('Failed to fetch weather history:', error);
        }

        // Format values
        const temp = weather.temperature !== null && weather.temperature !== undefined
            ? (useMetric
                ? `${Math.round(this.renderer.fahrenheitToCelsius(weather.temperature))}°C`
                : `${Math.round(weather.temperature)}°F`)
            : 'N/A';

        const dewpoint = weather.dew_point !== null && weather.dew_point !== undefined
            ? (useMetric
                ? `${Math.round(this.renderer.fahrenheitToCelsius(weather.dew_point))}°C`
                : `${Math.round(weather.dew_point)}°F`)
            : 'N/A';

        const humidity = weather.humidity !== null ? `${weather.humidity}%` : 'N/A';
        const pressure = weather.pressure !== null ? `${weather.pressure.toFixed(1)} mbar` : 'N/A';

        const windSpeed = weather.wind_speed !== null
            ? `${Math.round(weather.wind_speed)} mph (${Math.round(this.renderer.mphToKnots(weather.wind_speed))} kt)`
            : 'N/A';

        const windDir = weather.wind_direction !== null ? `${weather.wind_direction}°` : 'N/A';
        const windGust = weather.wind_gust !== null ? `${Math.round(weather.wind_gust)} mph` : 'N/A';

        // Fetch Zambretti forecast if station has pressure data
        let forecastHTML = '';
        if (weather.pressure !== null && weather.pressure !== undefined) {
            try {
                const response = await fetch(`/api/zambretti/${encodeURIComponent(station.callsign)}`);
                if (response.ok) {
                    const forecast = await response.json();
                    const trendIcon = forecast.trend === 'rising' ? '↑' :
                                     forecast.trend === 'falling' ? '↓' : '→';
                    const confidenceColor = forecast.confidence === 'high' ? '#28a745' :
                                           forecast.confidence === 'medium' ? '#ffc107' : '#6c757d';

                    forecastHTML = `
                        <div style="margin-top: 10px; padding: 8px; background: #f8f9fa; border-radius: 4px;">
                            <div style="font-weight: bold; font-size: 12px; color: #003399; margin-bottom: 4px;">
                                Forecast (${forecast.code}):
                            </div>
                            <div style="font-size: 13px; margin-bottom: 6px;">
                                ${forecast.forecast}
                            </div>
                            <div style="font-size: 11px; color: #666;">
                                Pressure ${trendIcon} ${forecast.trend}
                                <span style="color: ${confidenceColor};">(${forecast.confidence})</span>
                            </div>
                        </div>
                    `;
                }
            } catch (error) {
                console.warn('Failed to fetch Zambretti forecast:', error);
            }
        }

        // Generate visualizations
        const tempChart = this.generateTemperatureChart(weatherHistory, useMetric, weather.temperature);
        const windRose = this.generateWindRose(weatherHistory);

        // Build popup content with two-column layout
        const popupContent = `
            <div style="display: grid; grid-template-columns: 250px 220px; gap: 15px; min-width: 470px;">
                <!-- Left column: Current data -->
                <div>
                    <h3 style="margin: 0 0 10px 0; color: #003399;">
                        <a href="/station/${encodeURIComponent(station.callsign)}"
                           style="color: #003399; text-decoration: none;">
                            ${station.callsign}
                        </a>
                    </h3>
                    <div style="display: grid; grid-template-columns: auto 1fr; gap: 5px 10px; font-size: 13px;">
                        <strong>Temperature:</strong><span>${temp}</span>
                        <strong>Dew Point:</strong><span>${dewpoint}</span>
                        <strong>Humidity:</strong><span>${humidity}</span>
                        <strong>Pressure:</strong><span>${pressure}</span>
                        <strong>Wind:</strong><span>${windSpeed} @ ${windDir}</span>
                        ${weather.wind_gust !== null ? `<strong>Gust:</strong><span>${windGust}</span>` : ''}
                    </div>
                    ${forecastHTML}
                    ${weather.timestamp ? `
                        <div style="margin-top: 8px; font-size: 11px; color: #aaa;">
                            Updated: ${new Date(weather.timestamp).toLocaleString()}
                        </div>
                    ` : ''}
                </div>

                <!-- Right column: Visualizations -->
                <div>
                    <div style="margin-bottom: 15px;">
                        <div style="font-weight: bold; font-size: 11px; color: #003399; margin-bottom: 5px;">
                            Temperature Trend
                        </div>
                        ${tempChart}
                    </div>
                    <div>
                        <div style="font-weight: bold; font-size: 11px; color: #003399; margin-bottom: 5px;">
                            Wind Rose
                        </div>
                        ${windRose}
                    </div>
                </div>
            </div>
        `;

        // Create popup without fade animation (which was causing opacity: 0 issue)
        // Use autoClose: true so clicking another station replaces the popup
        // Use closeOnClick: true so clicking map background closes it
        // Event propagation is stopped above so the opening click won't close it
        L.popup({
            maxWidth: 550,
            closeButton: true,
            autoClose: true, // Close when another popup opens (clicking another station)
            closeOnClick: true, // Close when clicking map background
            className: 'weather-popup-no-fade' // Custom class to disable fade
        })
            .setLatLng([station.latitude, station.longitude])
            .setContent(popupContent)
            .openOn(this.map);
    }

    /**
     * Load weather stations from API
     */
    async loadWeatherStations() {
        try {
            const response = await fetch('/api/weather?sort_by=last');
            const data = await response.json();

            this.weatherStations = data.weather_stations.map(station => ({
                callsign: station.callsign,
                latitude: station.last_position?.latitude,
                longitude: station.last_position?.longitude,
                weather: station.last_weather,
                fullData: station  // Store full station data for popups
            })).filter(station => {
                // Only include stations with position and meaningful weather data
                if (!station.latitude || !station.longitude || !station.weather) {
                    return false;
                }

                const wx = station.weather;
                // Require at least one actual weather field (not just humidity-only or empty)
                const hasTemp = wx.temperature !== null && wx.temperature !== undefined;
                const hasPressure = wx.pressure !== null && wx.pressure !== undefined;
                const hasWind = wx.wind_speed !== null && wx.wind_speed !== undefined;

                return hasTemp || hasPressure || hasWind;
            });

            // Update station count in header
            const stationCount = document.getElementById('station-count');
            if (stationCount) {
                stationCount.textContent = `${this.weatherStations.length} Weather Stations`;
            }

            // Initial draw
            this.redraw();

            // Don't auto-fit bounds - keep the initial view (GPS or default)
            // User can manually zoom/pan to see all stations if needed
            // Auto-fitting often zooms way out and is disorienting

        } catch (error) {
            console.error('Failed to load weather stations:', error);
        }
    }

    /**
     * Redraw all station models on canvas
     */
    redraw() {
        if (!this.canvas || !this.ctx || !this.renderer) return;

        // Clear canvas
        this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);

        // Get map bounds for culling
        const bounds = this.map.getBounds();

        // Get the canvas offset in layer coordinates
        // The canvas is positioned at the top-left of the viewport in layer coords
        const canvasLayerPoint = this.map.containerPointToLayerPoint([0, 0]);

        // Draw each station
        for (const station of this.weatherStations) {
            // Cull stations outside viewport
            if (!bounds.contains([station.latitude, station.longitude])) {
                continue;
            }

            // Convert lat/lon to layer coordinates
            const stationLayerPoint = this.map.latLngToLayerPoint([station.latitude, station.longitude]);

            // Calculate position on canvas (relative to canvas's top-left)
            const canvasX = stationLayerPoint.x - canvasLayerPoint.x;
            const canvasY = stationLayerPoint.y - canvasLayerPoint.y;

            // Draw station model
            this.renderer.drawStationModel(canvasX, canvasY, station.weather, {
                showTemp: this.displayOptions.show_temp,
                showDewpoint: this.displayOptions.show_dewpoint,
                showWind: this.displayOptions.show_wind,
                showPressure: this.displayOptions.show_pressure,
                showLabel: this.displayOptions.show_labels,
                useMetric: this.displayOptions.use_metric
            });
        }
    }

    /**
     * Setup Server-Sent Events for real-time updates
     */
    setupSSE() {
        const statusEl = document.getElementById('connection-status');

        this.sseManager = new SSEManager('/api/events', {
            listeners: {
                weather_update: (event) => {
                    try {
                        const data = JSON.parse(event.data);
                        const callsign = data.callsign;

                        // Find and update station
                        const station = this.weatherStations.find(s => s.callsign === callsign);
                        if (station && data.weather) {
                            station.weather = data.weather;

                            // Update position if provided
                            if (data.position) {
                                station.latitude = data.position.latitude;
                                station.longitude = data.position.longitude;
                            }

                            this.redraw();
                        } else if (!station && data.weather && data.position) {
                            // New weather station
                            this.weatherStations.push({
                                callsign: callsign,
                                latitude: data.position.latitude,
                                longitude: data.position.longitude,
                                weather: data.weather,
                                fullData: data
                            });

                            // Update count
                            const stationCount = document.getElementById('station-count');
                            if (stationCount) {
                                stationCount.textContent = `${this.weatherStations.length} Weather Stations`;
                            }

                            this.redraw();
                        }
                    } catch (error) {
                        console.error('Error processing weather update:', error);
                    }
                },
            },
            onConnected: () => {
                if (statusEl) {
                    statusEl.textContent = 'Connected';
                    statusEl.className = 'status-item status-connected';
                }
            },
            onDisconnected: () => {
                if (statusEl) {
                    statusEl.textContent = 'Disconnected';
                    statusEl.className = 'status-item status-disconnected';
                }
            },
        });
    }
}

// Initialize app when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    const app = new WeatherMapApp();
    app.init();
});
