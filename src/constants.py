"""Constants and configuration for the radio console."""

# --- Version ---
VERSION = "0.9.1"

# --- Configuration ---
TNC_TCP_PORT = 8001
HEARTBEAT_INTERVAL = 10  # seconds
CONNECTION_TIMEOUT = 30  # seconds
TNC_RETRY_TIMEOUT = 3  # seconds before retransmit
TNC_MAX_RETRIES = 3

# UUIDs
RADIO_WRITE_UUID = "00001101-d102-11e1-9b23-00025b00a5a5"
RADIO_INDICATE_UUID = "00001102-d102-11e1-9b23-00025b00a5a5"
TNC_TX_UUID = "00000002-ba2a-46c9-ae49-01b0961f68bb"
TNC_RX_UUID = "00000003-ba2a-46c9-ae49-01b0961f68bb"
TNC_COMMAND_UUID = "0000fff3-0000-1000-8000-00805f9b34fb"
TNC_INDICATION_UUID = "0000fff4-0000-1000-8000-00805f9b34fb"

# Command Groups
CMD_GROUP_BASIC = 2

# Commands
CMD_READ_SETTINGS = 10
CMD_WRITE_SETTINGS = 11
CMD_GET_HT_STATUS = 20
CMD_READ_RF_CH = 13
CMD_WRITE_RF_CH = 14
CMD_GET_DEV_INFO = 4
CMD_READ_BSS_SETTINGS = 33
CMD_WRITE_BSS_SETTINGS = 34
CMD_GET_VOLUME = 22
CMD_SET_VOLUME = 23
CMD_SET_HT_POWER = 21
CMD_GET_POSITION = 76
CMD_SET_POSITION = 32

# Debug mode (can be changed at runtime)
DEBUG = False

# Debug level system (0-6)
# 0 = No debugging
# 1 = Reserved for future use
# 2 = Critical errors and important events
# 3 = Connection state changes, major operations
# 4 = Frame transmission/reception details
# 5 = Detailed protocol information, retransmissions
# 6 = Everything including BLE, config, hex dumps
DEBUG_LEVEL = 0

# Per-station debug filters (callsign -> debug_level)
# When set, overrides DEBUG_LEVEL for specific stations
# Example: {"K1MAL-7": 5} = debug level 5 for K1MAL-7 only
DEBUG_STATION_FILTERS = {}
