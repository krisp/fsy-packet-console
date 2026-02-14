/**
 * Digipeater Charts - Chart.js visualization functions for digipeater statistics
 */

/**
 * Format timestamp for chart axis label
 * @param {string} timestamp - ISO timestamp
 * @returns {string} Formatted time (HH:MM or MM/DD HH:MM)
 */
function formatDigipeaterTimeLabel(timestamp) {
    const date = new Date(timestamp);
    const hours = date.getHours().toString().padStart(2, '0');
    const minutes = date.getMinutes().toString().padStart(2, '0');

    // For data older than 24 hours, include date
    const now = new Date();
    const daysDiff = (now - date) / (1000 * 60 * 60 * 24);

    if (daysDiff > 1) {
        const month = (date.getMonth() + 1).toString().padStart(2, '0');
        const day = date.getDate().toString().padStart(2, '0');
        return `${month}/${day} ${hours}:${minutes}`;
    }

    return `${hours}:${minutes}`;
}

/**
 * Create hourly activity line chart with dual datasets
 * @param {string} canvasId - Canvas element ID
 * @param {Array} data - Array of time buckets with packet_count and unique_stations
 * @returns {Chart} Chart.js instance
 */
export function createHourlyActivityChart(canvasId, data) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) {
        console.error(`Canvas element ${canvasId} not found`);
        return null;
    }

    if (!data || data.length === 0) {
        console.warn('No activity data available for chart');
        return null;
    }

    // Format timestamps as readable labels
    const labels = data.map(bucket => formatDigipeaterTimeLabel(bucket.timestamp));

    return new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [
                {
                    label: 'Packets Digipeated',
                    data: data.map(bucket => bucket.packet_count || 0),
                    borderColor: 'rgb(75, 192, 192)',
                    backgroundColor: 'rgba(75, 192, 192, 0.1)',
                    tension: 0.4,
                    fill: true,
                    yAxisID: 'y'
                },
                {
                    label: 'Unique Stations',
                    data: data.map(bucket => bucket.unique_stations || 0),
                    borderColor: 'rgb(255, 159, 64)',
                    backgroundColor: 'rgba(255, 159, 64, 0.1)',
                    tension: 0.4,
                    fill: true,
                    yAxisID: 'y1'
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                mode: 'index',
                intersect: false
            },
            plugins: {
                legend: {
                    display: true,
                    position: 'top',
                    labels: {
                        color: '#fff',
                        font: {
                            size: 12
                        }
                    }
                },
                title: {
                    display: false
                },
                tooltip: {
                    callbacks: {
                        label: function(context) {
                            let label = context.dataset.label || '';
                            if (label) {
                                label += ': ';
                            }
                            label += Math.round(context.parsed.y);
                            return label;
                        }
                    }
                }
            },
            scales: {
                x: {
                    ticks: {
                        color: '#fff',
                        maxRotation: 45,
                        minRotation: 45
                    },
                    grid: {
                        color: '#333'
                    }
                },
                y: {
                    type: 'linear',
                    display: true,
                    position: 'left',
                    title: {
                        display: true,
                        text: 'Packets',
                        color: '#fff'
                    },
                    ticks: {
                        color: '#fff',
                        precision: 0
                    },
                    grid: {
                        color: '#333'
                    }
                },
                y1: {
                    type: 'linear',
                    display: true,
                    position: 'right',
                    title: {
                        display: true,
                        text: 'Stations',
                        color: '#fff'
                    },
                    ticks: {
                        color: '#fff',
                        precision: 0
                    },
                    grid: {
                        drawOnChartArea: false,
                        color: '#333'
                    }
                }
            }
        }
    });
}

/**
 * Create top stations horizontal bar chart
 * @param {string} canvasId - Canvas element ID
 * @param {Array} data - Array of {callsign, count, last_heard}
 * @param {Function} onStationClick - Optional callback when bar is clicked
 * @returns {Chart} Chart.js instance
 */
export function createTopStationsChart(canvasId, data, onStationClick = null) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) {
        console.error(`Canvas element ${canvasId} not found`);
        return null;
    }

    if (!data || data.length === 0) {
        console.warn('No top stations data available for chart');
        return null;
    }

    // Sort by count descending and take top 10
    const topStations = [...data]
        .sort((a, b) => b.count - a.count)
        .slice(0, 10);

    const labels = topStations.map(s => s.callsign);
    const counts = topStations.map(s => s.count);

    const chart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [{
                label: 'Packets Digipeated',
                data: counts,
                backgroundColor: 'rgba(54, 162, 235, 0.6)',
                borderColor: 'rgb(54, 162, 235)',
                borderWidth: 1
            }]
        },
        options: {
            indexAxis: 'y',
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    display: false
                },
                title: {
                    display: false
                },
                tooltip: {
                    callbacks: {
                        label: function(context) {
                            return `${context.parsed.x} packets`;
                        }
                    }
                }
            },
            scales: {
                x: {
                    beginAtZero: true,
                    ticks: {
                        color: '#fff',
                        precision: 0
                    },
                    grid: {
                        color: '#333'
                    },
                    title: {
                        display: true,
                        text: 'Packet Count',
                        color: '#fff'
                    }
                },
                y: {
                    ticks: {
                        color: '#fff'
                    },
                    grid: {
                        color: '#333'
                    }
                }
            },
            onClick: onStationClick ? (event, elements) => {
                if (elements.length > 0) {
                    const index = elements[0].index;
                    const callsign = labels[index];
                    onStationClick(callsign);
                }
            } : undefined
        }
    });

    return chart;
}

/**
 * Create path usage doughnut chart
 * @param {string} canvasId - Canvas element ID
 * @param {Object} data - Object with path_type as keys and counts as values
 * @returns {Chart} Chart.js instance
 */
export function createPathUsageChart(canvasId, data) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) {
        console.error(`Canvas element ${canvasId} not found`);
        return null;
    }

    if (!data || Object.keys(data).length === 0) {
        console.warn('No path usage data available for chart');
        return null;
    }

    const labels = Object.keys(data);
    const values = Object.values(data);

    // Color palette for different path types
    const colors = [
        'rgba(255, 99, 132, 0.8)',   // WIDE1-1 - Red
        'rgba(54, 162, 235, 0.8)',   // WIDE2-2 - Blue
        'rgba(255, 206, 86, 0.8)',   // WIDE2-1 - Yellow
        'rgba(75, 192, 192, 0.8)',   // Direct - Teal
        'rgba(153, 102, 255, 0.8)',  // Other - Purple
    ];

    const borderColors = colors.map(c => c.replace('0.8', '1'));

    return new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels: labels,
            datasets: [{
                label: 'Packet Count',
                data: values,
                backgroundColor: colors.slice(0, labels.length),
                borderColor: borderColors.slice(0, labels.length),
                borderWidth: 2
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    display: true,
                    position: 'right',
                    labels: {
                        color: '#fff',
                        font: {
                            size: 12
                        },
                        generateLabels: function(chart) {
                            const data = chart.data;
                            const total = data.datasets[0].data.reduce((a, b) => a + b, 0);

                            return data.labels.map((label, i) => {
                                const value = data.datasets[0].data[i];
                                const percent = ((value / total) * 100).toFixed(1);
                                return {
                                    text: `${label}: ${value} (${percent}%)`,
                                    fillStyle: data.datasets[0].backgroundColor[i],
                                    fontColor: '#fff',  // Chart.js v2
                                    color: '#fff',      // Chart.js v3+
                                    hidden: false,
                                    index: i
                                };
                            });
                        }
                    }
                },
                title: {
                    display: false
                },
                tooltip: {
                    callbacks: {
                        label: function(context) {
                            const label = context.label || '';
                            const value = context.parsed;
                            const total = context.dataset.data.reduce((a, b) => a + b, 0);
                            const percent = ((value / total) * 100).toFixed(1);
                            return `${label}: ${value} packets (${percent}%)`;
                        }
                    }
                }
            }
        }
    });
}

/**
 * Render activity heatmap on canvas (7x24 grid)
 * @param {string} canvasId - Canvas element ID
 * @param {Array} data - 2D grid array [7 rows x 24 cols] with packet counts
 * @returns {Object} Heatmap metadata for later updates
 */
export function renderActivityHeatmap(canvasId, data) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) {
        console.error(`Canvas element ${canvasId} not found`);
        return null;
    }

    const ctx = canvas.getContext('2d');

    // Canvas dimensions
    const width = canvas.width;
    const height = canvas.height;

    // Grid dimensions (24 hours x 7 days)
    const cols = 24; // Hours
    const rows = 7;  // Days of week

    const cellWidth = width / cols;
    const cellHeight = height / rows;

    // Day labels (Monday-first to match Python's weekday())
    // Backend uses: 0=Monday, 1=Tuesday, ..., 5=Saturday, 6=Sunday
    const dayLabels = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

    // Use provided grid directly (already in 7x24 format from API)
    const grid = data && Array.isArray(data) && data.length === 7
        ? data
        : Array(rows).fill(null).map(() => Array(cols).fill(0));

    // Find max count for color scaling
    const maxCount = Math.max(1, ...grid.flat());

    // Clear canvas
    ctx.clearRect(0, 0, width, height);

    // Draw grid cells
    for (let row = 0; row < rows; row++) {
        for (let col = 0; col < cols; col++) {
            const count = grid[row][col];
            const intensity = count / maxCount;

            // Color scale: dark blue (no activity) to bright green (high activity)
            const r = Math.floor(75 + (intensity * 117)); // 75 -> 192
            const g = Math.floor(192 * intensity);         // 0 -> 192
            const b = Math.floor(75 - (intensity * 75));   // 75 -> 0

            ctx.fillStyle = `rgba(${r}, ${g}, ${b}, 0.8)`;

            const x = col * cellWidth;
            const y = row * cellHeight;

            ctx.fillRect(x, y, cellWidth - 1, cellHeight - 1);

            // Add count text for cells with activity
            if (count > 0 && cellWidth > 20 && cellHeight > 15) {
                ctx.fillStyle = '#fff';
                ctx.font = '10px monospace';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillText(count.toString(), x + cellWidth / 2, y + cellHeight / 2);
            }
        }
    }

    // Draw labels (hours on top, days on left)
    ctx.fillStyle = '#fff';
    ctx.font = '11px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'bottom';

    // Hour labels (every 4 hours)
    for (let hour = 0; hour < cols; hour += 4) {
        const x = hour * cellWidth + cellWidth / 2;
        const label = hour.toString().padStart(2, '0');
        ctx.fillText(label, x, height - 2);
    }

    // Day labels
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (let day = 0; day < rows; day++) {
        const y = day * cellHeight + cellHeight / 2;
        ctx.fillText(dayLabels[day], width - 2, y);
    }

    return {
        grid: grid,
        maxCount: maxCount,
        update: (newData) => renderActivityHeatmap(canvasId, newData)
    };
}

/**
 * Update chart data incrementally without full redraw
 * @param {Chart} chart - Chart.js instance
 * @param {Object} newData - New data point(s) to add
 */
export function incrementChartData(chart, newData) {
    if (!chart || !newData) {
        return;
    }

    // Handle different chart types
    if (chart.config.type === 'line') {
        // Add new data point to line chart
        if (newData.timestamp && newData.value !== undefined) {
            chart.data.labels.push(formatDigipeaterTimeLabel(newData.timestamp));
            chart.data.datasets.forEach((dataset, index) => {
                const value = index === 0 ? newData.value : newData.value2 || 0;
                dataset.data.push(value);
            });

            // Keep only last 50 points to prevent performance issues
            if (chart.data.labels.length > 50) {
                chart.data.labels.shift();
                chart.data.datasets.forEach(dataset => dataset.data.shift());
            }

            chart.update('none'); // Update without animation
        }
    } else if (chart.config.type === 'bar') {
        // Update bar chart data (for top stations)
        if (newData.callsign && newData.count !== undefined) {
            const index = chart.data.labels.indexOf(newData.callsign);
            if (index >= 0) {
                // Update existing station
                chart.data.datasets[0].data[index] = newData.count;
            } else {
                // Add new station and re-sort
                chart.data.labels.push(newData.callsign);
                chart.data.datasets[0].data.push(newData.count);

                // Sort by count
                const combined = chart.data.labels.map((label, i) => ({
                    label: label,
                    value: chart.data.datasets[0].data[i]
                }));
                combined.sort((a, b) => b.value - a.value);

                // Keep top 10
                combined.splice(10);

                chart.data.labels = combined.map(c => c.label);
                chart.data.datasets[0].data = combined.map(c => c.value);
            }

            chart.update('none');
        }
    } else if (chart.config.type === 'doughnut') {
        // Update doughnut chart (for path usage)
        if (newData.path_type && newData.count !== undefined) {
            const index = chart.data.labels.indexOf(newData.path_type);
            if (index >= 0) {
                chart.data.datasets[0].data[index] += newData.count;
            } else {
                chart.data.labels.push(newData.path_type);
                chart.data.datasets[0].data.push(newData.count);
            }

            chart.update('none');
        }
    }
}
