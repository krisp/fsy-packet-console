/**
 * APRS Charts - Chart.js wrapper for weather visualization
 */

/**
 * Format timestamp for chart axis label
 * @param {string} timestamp - ISO timestamp
 * @returns {string} Formatted time (HH:MM)
 */
function formatChartTimeLabel(timestamp) {
    const date = new Date(timestamp);
    const hours = date.getHours().toString().padStart(2, '0');
    const minutes = date.getMinutes().toString().padStart(2, '0');
    return `${hours}:${minutes}`;
}

/**
 * Create a temperature history chart
 * @param {string} canvasId - Canvas element ID
 * @param {Array} weatherHistory - Array of weather objects with timestamps and temperature
 * @returns {Chart} Chart.js instance
 */
export function createTemperatureChart(canvasId, weatherHistory) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) {
        console.error(`Canvas element ${canvasId} not found`);
        return null;
    }

    // Filter out entries without temperature data
    const data = weatherHistory.filter(wx => wx.temperature !== null);

    if (data.length === 0) {
        console.warn('No temperature data available for chart');
        return null;
    }

    // Format timestamps as readable labels
    const labels = data.map(wx => formatChartTimeLabel(wx.timestamp));

    return new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                label: 'Temperature (°F)',
                data: data.map(wx => wx.temperature),
                borderColor: 'rgb(255, 99, 132)',
                backgroundColor: 'rgba(255, 99, 132, 0.1)',
                tension: 0.4,
                fill: true
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    display: false
                },
                title: {
                    display: false
                }
            },
            scales: {
                x: {
                    ticks: {
                        color: '#888',
                        maxRotation: 45,
                        minRotation: 45
                    },
                    grid: {
                        color: '#333'
                    }
                },
                y: {
                    title: {
                        display: true,
                        text: 'Temperature (°F)',
                        color: '#888'
                    },
                    ticks: {
                        color: '#888'
                    },
                    grid: {
                        color: '#333'
                    }
                }
            }
        }
    });
}

/**
 * Create a humidity history chart
 * @param {string} canvasId - Canvas element ID
 * @param {Array} weatherHistory - Array of weather objects with timestamps and humidity
 * @returns {Chart} Chart.js instance
 */
export function createHumidityChart(canvasId, weatherHistory) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) {
        console.error(`Canvas element ${canvasId} not found`);
        return null;
    }

    const data = weatherHistory.filter(wx => wx.humidity !== null);

    if (data.length === 0) {
        return null;
    }

    // Format timestamps as readable labels
    const labels = data.map(wx => formatChartTimeLabel(wx.timestamp));

    return new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                label: 'Humidity (%)',
                data: data.map(wx => wx.humidity),
                borderColor: 'rgb(54, 162, 235)',
                backgroundColor: 'rgba(54, 162, 235, 0.1)',
                tension: 0.4,
                fill: true
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    display: false
                }
            },
            scales: {
                x: {
                    ticks: {
                        color: '#888',
                        maxRotation: 45,
                        minRotation: 45
                    },
                    grid: {
                        color: '#333'
                    }
                },
                y: {
                    min: 0,
                    max: 100,
                    title: {
                        display: true,
                        text: 'Humidity (%)',
                        color: '#888'
                    },
                    ticks: {
                        color: '#888'
                    },
                    grid: {
                        color: '#333'
                    }
                }
            }
        }
    });
}

/**
 * Create a wind rose chart (simplified version)
 * @param {string} canvasId - Canvas element ID
 * @param {Array} weatherHistory - Array of weather objects with wind data
 * @returns {Chart} Chart.js instance
 */
export function createWindRose(canvasId, weatherHistory) {
    // TODO: Implement proper wind rose visualization
    // For now, create a simple wind speed/direction chart

    const ctx = document.getElementById(canvasId);
    if (!ctx) {
        console.error(`Canvas element ${canvasId} not found`);
        return null;
    }

    const data = weatherHistory.filter(wx => wx.wind_speed !== null && wx.wind_direction !== null);

    if (data.length === 0) {
        return null;
    }

    return new Chart(ctx, {
        type: 'scatter',
        data: {
            datasets: [{
                label: 'Wind',
                data: data.map(wx => ({
                    x: wx.wind_direction,
                    y: wx.wind_speed
                })),
                backgroundColor: 'rgba(75, 192, 192, 0.6)',
                borderColor: 'rgb(75, 192, 192)',
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    display: false
                },
                title: {
                    display: true,
                    text: 'Wind Direction vs Speed',
                    color: '#888'
                }
            },
            scales: {
                x: {
                    title: {
                        display: true,
                        text: 'Direction (°)',
                        color: '#888'
                    },
                    min: 0,
                    max: 360,
                    ticks: {
                        color: '#888'
                    },
                    grid: {
                        color: '#333'
                    }
                },
                y: {
                    title: {
                        display: true,
                        text: 'Speed (mph)',
                        color: '#888'
                    },
                    min: 0,
                    ticks: {
                        color: '#888'
                    },
                    grid: {
                        color: '#333'
                    }
                }
            }
        }
    });
}

/**
 * Format timestamp for display
 * @param {string} timestamp - ISO timestamp
 * @returns {string} Formatted time string
 */
export function formatTime(timestamp) {
    const date = new Date(timestamp);
    const now = new Date();
    const diff = now - date;

    // If less than 1 hour, show minutes ago
    if (diff < 3600000) {
        const minutes = Math.floor(diff / 60000);
        return minutes === 0 ? 'Just now' : `${minutes}m ago`;
    }

    // If less than 24 hours, show hours ago
    if (diff < 86400000) {
        const hours = Math.floor(diff / 3600000);
        return `${hours}h ago`;
    }

    // Otherwise show date and time
    return date.toLocaleString();
}
