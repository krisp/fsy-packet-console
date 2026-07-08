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
