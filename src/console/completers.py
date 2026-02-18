"""Tab completion for TNC and console commands."""

from prompt_toolkit.completion import Completer, Completion


class TNCCompleter(Completer):
    """Tab completion for TNC mode commands."""

    def get_completions(self, document, complete_event):
        """Generate completions for TNC commands.

        Args:
            document: Current document (input text)
            complete_event: Completion event

        Yields:
            Completion objects for matching TNC commands
        """
        text = document.text_before_cursor.upper()
        words = text.split()

        # TNC-2 commands
        tnc_commands = [
            "CONNECT",
            "DISCONNECT",
            "CONVERSE",
            "MYCALL",
            "MYALIAS",
            "MYLOCATION",
            "UNPROTO",
            "MONITOR",
            "AUTO_ACK",
            "BEACON",
            "DIGIPEATER",
            "DIGI",
            "RETRY",
            "RETRY_FAST",
            "RETRY_SLOW",
            "DISPLAY",
            "STATUS",
            "RESET",
            "HARDRESET",
            "POWERCYCLE",
            "DEBUGFRAMES",
            "AGWPE_HOST",
            "AGWPE_PORT",
            "TNC_HOST",
            "TNC_PORT",
            "WEBUI_HOST",
            "WEBUI_PORT",
            "WEBUI_PASSWORD",
            "WX_ENABLE",
            "WX_BACKEND",
            "WX_ADDRESS",
            "WX_PORT",
            "WX_INTERVAL",
            "WX_AVERAGE_WIND",
            "QUIT",
            "EXIT",
        ]

        if not words or (len(words) == 1 and not text.endswith(" ")):
            word = words[0] if words else ""
            for cmd in tnc_commands:
                if cmd.startswith(word):
                    yield Completion(
                        cmd,
                        start_position=-len(word),
                        display=cmd,
                        display_meta=self._get_tnc_help(cmd),
                    )

    def _get_tnc_help(self, cmd):
        """Get brief help for TNC command.

        Args:
            cmd: TNC command name

        Returns:
            Brief help string
        """
        help_text = {
            "CONNECT": "Connect to station",
            "DISCONNECT": "Disconnect from station",
            "CONVERSE": "Enter conversation mode",
            "MYCALL": "Set/show my callsign",
            "MYALIAS": "Set/show my alias",
            "MYLOCATION": "Set manual position (Maidenhead grid, e.g., FN31pr)",
            "RADIO_MAC": "Set Bluetooth MAC address for BLE radio (e.g., 38:D2:00:01:62:C2)",
            "UNPROTO": "Set unproto destination",
            "MONITOR": "Toggle monitor mode",
            "AUTO_ACK": "Auto-acknowledge APRS messages (ON/OFF)",
            "BEACON": "GPS beacon (ON/OFF/INTERVAL/PATH/SYMBOL/COMMENT/NOW)",
            "DIGIPEATER": "Digipeater mode (ON/OFF/SELF) - repeats direct packets",
            "DIGI": "Digipeater mode (ON/OFF/SELF) - short alias",
            "RETRY": "Set max retry attempts (1-10)",
            "RETRY_FAST": "Fast retry timeout in seconds (5-300) for non-digipeated messages",
            "RETRY_SLOW": "Slow retry timeout in seconds (60-86400) for digipeated messages",
            "DISPLAY": "Toggle display mode",
            "STATUS": "Show TNC status",
            "RESET": "Reset TNC settings",
            "HARDRESET": "Hard reset radio",
            "POWERCYCLE": "Power cycle radio",
            "DEBUGFRAMES": "Toggle frame debugging",
            "AGWPE_HOST": "Set AGWPE bind address (0.0.0.0=all, 127.0.0.1=localhost)",
            "AGWPE_PORT": "Set AGWPE server port (default: 8000)",
            "TNC_HOST": "Set TNC bridge bind address (0.0.0.0=all, 127.0.0.1=localhost)",
            "TNC_PORT": "Set TNC bridge port (default: 8001)",
            "WEBUI_HOST": "Set Web UI bind address (0.0.0.0=all, 127.0.0.1=localhost)",
            "WEBUI_PORT": "Set Web UI port (default: 8002)",
            "WEBUI_PASSWORD": "Set password for Web UI POST endpoints (empty = disabled)",
            "WX_ENABLE": "Enable/disable weather station (ON/OFF)",
            "WX_BACKEND": "Set weather station backend (ecowitt, davis, etc.)",
            "WX_ADDRESS": "Set weather station IP or serial port",
            "WX_PORT": "Set weather station port (blank = auto)",
            "WX_INTERVAL": "Set update interval in seconds (30-3600)",
            "WX_AVERAGE_WIND": "Average wind over beacon interval (ON/OFF)",
            "QUIT": "Exit TNC mode",
            "EXIT": "Exit TNC mode",
        }
        return help_text.get(cmd, "")


class CommandCompleter(Completer):
    """Tab completion for radio console commands."""

    def __init__(self, command_processor):
        """Initialize with reference to command processor.

        Args:
            command_processor: CommandProcessor instance to get available commands
        """
        self.command_processor = command_processor

    def get_completions(self, document, complete_event):
        """Generate completions for the current input.

        Args:
            document: Current document (input text)
            complete_event: Completion event

        Yields:
            Completion objects for matching commands
        """
        text = document.text_before_cursor
        words = text.split()

        # If empty or just whitespace, show all commands
        if not words or (len(words) == 1 and not text.endswith(" ")):
            # Completing the first word (command)
            word = words[0] if words else ""

            # Get base commands
            commands = sorted(self.command_processor.commands.keys())

            # Mode-specific filtering
            if self.command_processor.console_mode == "aprs":
                # APRS mode: add APRS subcommands as top-level, hide radio commands
                aprs_subcommands = ["message", "msg", "station", "wx", "weather"]
                commands = sorted(set(commands + aprs_subcommands))

                # Hide radio-specific commands (keep "radio" for mode switching if BLE)
                radio_commands = ["status", "health", "vfo", "setvfo", "active", "dual",
                                "scan", "squelch", "volume", "channel", "list", "power",
                                "freq", "bss", "setbss", "poweron", "poweroff", "scan_ble",
                                "notifications", "gps"]
                commands = [c for c in commands if c not in radio_commands]

                # In serial mode, also hide the "radio" command (can't switch to radio mode)
                if self.command_processor.serial_mode:
                    commands = [c for c in commands if c != "radio"]

            elif self.command_processor.console_mode == "radio":
                # Radio mode: don't show APRS subcommands as top-level (keep "aprs" for mode switching)
                pass  # APRS subcommands stay hidden, full commands shown normally

            # Filter and yield matching commands
            for cmd in commands:
                if cmd.startswith(word.lower()):
                    yield Completion(
                        cmd,
                        start_position=-len(word),
                        display=cmd,
                        display_meta=self._get_command_help(cmd),
                    )

        # Special completion for multi-word commands
        elif len(words) >= 1:
            first_word = words[0].lower()

            # APRS command completions
            if first_word == "aprs":
                if len(words) == 1 or (
                    len(words) == 2 and not text.endswith(" ")
                ):
                    # Complete aprs subcommands
                    subcommands = [
                        "message",
                        "msg",
                        "wx",
                        "weather",
                        "position",
                        "pos",
                        "station",
                        "database",
                        "db",
                    ]
                    word = words[1] if len(words) == 2 else ""
                    for sub in subcommands:
                        if sub.startswith(word):
                            yield Completion(
                                sub, start_position=-len(word), display=sub
                            )
                elif len(words) >= 2:
                    subcmd = words[1].lower()
                    if subcmd in ("message", "msg"):
                        if len(words) == 2 or (
                            len(words) == 3 and not text.endswith(" ")
                        ):
                            # Complete message actions
                            actions = ["read", "send", "clear", "monitor"]
                            word = words[2] if len(words) == 3 else ""
                            for action in actions:
                                if action.startswith(word):
                                    yield Completion(
                                        action,
                                        start_position=-len(word),
                                        display=action,
                                    )
                        elif len(words) >= 3:
                            action = words[2].lower()
                            if action == "monitor":
                                if len(words) == 3 or (
                                    len(words) == 4 and not text.endswith(" ")
                                ):
                                    # Complete monitor subactions
                                    subactions = ["list"]
                                    word = words[3] if len(words) == 4 else ""
                                    for subaction in subactions:
                                        if subaction.startswith(word):
                                            yield Completion(
                                                subaction,
                                                start_position=-len(word),
                                                display=subaction,
                                            )
                    elif subcmd in ("wx", "weather"):
                        if len(words) == 2 or (
                            len(words) == 3 and not text.endswith(" ")
                        ):
                            # Complete wx actions
                            actions = ["list"]
                            word = words[2] if len(words) == 3 else ""
                            for action in actions:
                                if action.startswith(word):
                                    yield Completion(
                                        action,
                                        start_position=-len(word),
                                        display=action,
                                    )
                        elif len(words) >= 3:
                            # Complete sort options for "aprs wx list"
                            action = words[2].lower()
                            if action == "list":
                                if len(words) == 3 or (
                                    len(words) == 4 and not text.endswith(" ")
                                ):
                                    sort_options = [
                                        "last",
                                        "name",
                                        "temp",
                                        "humidity",
                                        "pressure",
                                    ]
                                    word = words[3] if len(words) == 4 else ""
                                    for option in sort_options:
                                        if option.startswith(word):
                                            # Add descriptive meta text
                                            meta = {
                                                "last": "Most recent first",
                                                "name": "Alphabetically by callsign",
                                                "temp": "Highest temperature first",
                                                "humidity": "Highest humidity first",
                                                "pressure": "Highest pressure first",
                                            }.get(option, "")
                                            yield Completion(
                                                option,
                                                start_position=-len(word),
                                                display=option,
                                                display_meta=meta,
                                            )
                    elif subcmd in ("position", "pos"):
                        if len(words) == 2 or (
                            len(words) == 3 and not text.endswith(" ")
                        ):
                            # Complete position actions
                            actions = ["list"]
                            word = words[2] if len(words) == 3 else ""
                            for action in actions:
                                if action.startswith(word):
                                    yield Completion(
                                        action,
                                        start_position=-len(word),
                                        display=action,
                                    )
                    elif subcmd == "station":
                        if len(words) == 2 or (
                            len(words) == 3 and not text.endswith(" ")
                        ):
                            # Complete station actions
                            actions = ["list", "show"]
                            word = words[2] if len(words) == 3 else ""
                            for action in actions:
                                if action.startswith(word):
                                    yield Completion(
                                        action,
                                        start_position=-len(word),
                                        display=action,
                                    )
                        elif len(words) >= 3 and words[2].lower() == "show":
                            # Complete with known station callsigns
                            if len(words) == 3 or (
                                len(words) == 4 and not text.endswith(" ")
                            ):
                                word = words[3] if len(words) == 4 else ""
                                stations = (
                                    self.command_processor.aprs_manager.get_all_stations()
                                )
                                for station in stations:
                                    if station.callsign.lower().startswith(
                                        word.lower()
                                    ):
                                        yield Completion(
                                            station.callsign,
                                            start_position=-len(word),
                                            display=station.callsign,
                                        )
                        elif len(words) >= 3 and words[2].lower() == "list":
                            # Complete sort order options for station list
                            if len(words) == 3 or (
                                len(words) == 4 and not text.endswith(" ")
                            ):
                                word = words[3] if len(words) == 4 else ""
                                sort_options = [
                                    (
                                        "name",
                                        "Sort alphabetically by callsign",
                                    ),
                                    (
                                        "packets",
                                        "Sort by packet count (highest first)",
                                    ),
                                    (
                                        "last",
                                        "Sort by last heard (most recent first)",
                                    ),
                                    (
                                        "hops",
                                        "Sort by hop count (direct RF first)",
                                    ),
                                ]
                                for option, meta in sort_options:
                                    if option.startswith(word.lower()):
                                        yield Completion(
                                            option,
                                            start_position=-len(word),
                                            display=option,
                                            display_meta=meta,
                                        )
                    elif subcmd in ("database", "db"):
                        if len(words) == 2 or (
                            len(words) == 3 and not text.endswith(" ")
                        ):
                            # Complete database actions
                            actions = ["clear", "prune"]
                            word = words[2] if len(words) == 3 else ""
                            for action in actions:
                                if action.startswith(word):
                                    yield Completion(
                                        action,
                                        start_position=-len(word),
                                        display=action,
                                    )

            # APRS subcommands as top-level commands (in APRS mode)
            # Handle "message ?", "station ?", etc. when used without "aprs" prefix
            elif self.command_processor.console_mode == "aprs" and first_word in ("message", "msg", "station", "wx", "weather"):
                # Redirect to the same logic as "aprs <subcommand>"
                # Treat first_word as if it were the second word after "aprs"
                subcmd = first_word

                if subcmd in ("message", "msg"):
                    if len(words) == 1 or (len(words) == 2 and not text.endswith(" ")):
                        # Complete message actions
                        actions = ["read", "send", "clear", "monitor"]
                        word = words[1] if len(words) == 2 else ""
                        for action in actions:
                            if action.startswith(word):
                                meta = {
                                    "read": "Read messages addressed to you",
                                    "send": "Send APRS message to callsign",
                                    "clear": "Clear read messages",
                                    "monitor": "View all monitored messages"
                                }.get(action, "")
                                yield Completion(
                                    action,
                                    start_position=-len(word),
                                    display=action,
                                    display_meta=meta,
                                )
                    elif len(words) >= 2:
                        action = words[1].lower()
                        if action == "monitor":
                            if len(words) == 2 or (len(words) == 3 and not text.endswith(" ")):
                                # Complete monitor subactions
                                subactions = ["list"]
                                word = words[2] if len(words) == 3 else ""
                                for subaction in subactions:
                                    if subaction.startswith(word):
                                        yield Completion(
                                            subaction,
                                            start_position=-len(word),
                                            display=subaction,
                                            display_meta="List all monitored messages",
                                        )

                elif subcmd in ("wx", "weather"):
                    if len(words) == 1 or (len(words) == 2 and not text.endswith(" ")):
                        # Complete wx actions
                        actions = ["list"]
                        word = words[1] if len(words) == 2 else ""
                        for action in actions:
                            if action.startswith(word):
                                yield Completion(
                                    action,
                                    start_position=-len(word),
                                    display=action,
                                    display_meta="List weather stations",
                                )
                    elif len(words) >= 2:
                        # Complete sort options for "wx list"
                        action = words[1].lower()
                        if action == "list":
                            if len(words) == 2 or (len(words) == 3 and not text.endswith(" ")):
                                sort_options = ["last", "name", "temp", "humidity", "pressure"]
                                word = words[2] if len(words) == 3 else ""
                                for option in sort_options:
                                    if option.startswith(word):
                                        meta = {
                                            "last": "Most recent first",
                                            "name": "Alphabetically by callsign",
                                            "temp": "Highest temperature first",
                                            "humidity": "Highest humidity first",
                                            "pressure": "Highest pressure first",
                                        }.get(option, "")
                                        yield Completion(
                                            option,
                                            start_position=-len(word),
                                            display=option,
                                            display_meta=meta,
                                        )

                elif subcmd == "station":
                    if len(words) == 1 or (len(words) == 2 and not text.endswith(" ")):
                        # Complete station actions
                        actions = ["list", "show"]
                        word = words[1] if len(words) == 2 else ""
                        for action in actions:
                            if action.startswith(word):
                                meta = {
                                    "list": "List all heard stations",
                                    "show": "Show detailed station info",
                                }.get(action, "")
                                yield Completion(
                                    action,
                                    start_position=-len(word),
                                    display=action,
                                    display_meta=meta,
                                )
                    elif len(words) >= 2 and words[1].lower() == "show":
                        # Complete with known station callsigns
                        if len(words) == 2 or (len(words) == 3 and not text.endswith(" ")):
                            word = words[2] if len(words) == 3 else ""
                            stations = self.command_processor.aprs_manager.get_all_stations()
                            for station in stations:
                                if station.callsign.lower().startswith(word.lower()):
                                    yield Completion(
                                        station.callsign,
                                        start_position=-len(word),
                                        display=station.callsign,
                                    )
                    elif len(words) >= 2 and words[1].lower() == "list":
                        # Complete sort options for "station list"
                        if len(words) == 2 or (len(words) == 3 and not text.endswith(" ")):
                            sort_options = ["last", "name", "packets", "hops"]
                            word = words[2] if len(words) == 3 else ""
                            for option in sort_options:
                                if option.startswith(word):
                                    meta = {
                                        "last": "Most recent first",
                                        "name": "Alphabetically by callsign",
                                        "packets": "Most packets first",
                                        "hops": "Fewest hops first",
                                    }.get(option, "")
                                    yield Completion(
                                        option,
                                        start_position=-len(word),
                                        display=option,
                                        display_meta=meta,
                                    )

            # VFO completions
            elif first_word in ("vfo", "setvfo"):
                if len(words) == 1 or (
                    len(words) == 2 and not text.endswith(" ")
                ):
                    vfos = ["a", "b"]
                    word = words[1] if len(words) == 2 else ""
                    for vfo in vfos:
                        if vfo.startswith(word.lower()):
                            yield Completion(
                                vfo.upper(),
                                start_position=-len(word),
                                display=vfo.upper(),
                            )

            # Power completions
            elif first_word == "power":
                if len(words) == 1 or (
                    len(words) == 2 and not text.endswith(" ")
                ):
                    levels = ["high", "medium", "low"]
                    word = words[1] if len(words) == 2 else ""
                    for level in levels:
                        if level.startswith(word.lower()):
                            yield Completion(
                                level, start_position=-len(word), display=level
                            )

            # Debug level completions
            elif first_word == "debug":
                if len(words) == 1 or (
                    len(words) == 2 and not text.endswith(" ")
                ):
                    level_meta = {
                        "0": "Off (no debug output)",
                        "1": "TNC monitor",
                        "2": "Critical errors and events",
                        "3": "Connection state changes",
                        "4": "Frame transmission/reception",
                        "5": "Protocol details, retransmissions",
                        "6": "Everything (BLE, config, hex dumps)",
                        "dump": "Dump frame history",
                        "filter": "Show/set station-specific debug filters",
                    }
                    # Add 'dump' and 'filter' to completions
                    options = ["0", "1", "2", "3", "4", "5", "6", "dump", "filter"]
                    word = words[1] if len(words) == 2 else ""
                    for option in options:
                        if option.startswith(word.lower()):
                            yield Completion(
                                option,
                                start_position=-len(word),
                                display=option,
                                display_meta=level_meta[option],
                            )
                elif len(words) >= 2 and words[1].lower() == "dump":
                    # After "debug dump", suggest "brief", "detail", or "watch"
                    if len(words) == 2 or (
                        len(words) >= 3 and not text.endswith(" ")
                    ):
                        word = words[-1] if len(words) >= 3 else ""
                        if "brief".startswith(word.lower()):
                            yield Completion(
                                "brief",
                                start_position=-len(word),
                                display="brief",
                                display_meta="compact hex output",
                            )
                        if "detail".startswith(word.lower()):
                            yield Completion(
                                "detail",
                                start_position=-len(word),
                                display="detail",
                                display_meta="Wireshark-style protocol analysis",
                            )
                        if "watch".startswith(word.lower()):
                            yield Completion(
                                "watch",
                                start_position=-len(word),
                                display="watch",
                                display_meta="live frame analysis (ESC to exit)",
                            )
                elif len(words) >= 2 and words[1].lower() == "filter":
                    # After "debug filter", suggest "clear"
                    if len(words) == 2 or (
                        len(words) == 3 and not text.endswith(" ")
                    ):
                        word = words[2] if len(words) == 3 else ""
                        if "clear".startswith(word.lower()):
                            yield Completion(
                                "clear",
                                start_position=-len(word),
                                display="clear",
                                display_meta="Clear all station filters",
                            )

            # PWS (Personal Weather Station) completions
            elif first_word == "pws":
                if len(words) == 1 or (
                    len(words) == 2 and not text.endswith(" ")
                ):
                    subcommands_meta = {
                        "show": "Display current weather data",
                        "fetch": "Fetch fresh weather data now",
                        "connect": "Connect to weather station",
                        "disconnect": "Disconnect from weather station",
                        "test": "Test connection to weather station",
                    }
                    word = words[1] if len(words) == 2 else ""
                    for subcmd, meta in subcommands_meta.items():
                        if subcmd.startswith(word.lower()):
                            yield Completion(
                                subcmd,
                                start_position=-len(word),
                                display=subcmd,
                                display_meta=meta,
                            )

            # TNC command completions
            elif first_word == "tnc":
                if len(words) == 1 or (
                    len(words) == 2 and not text.endswith(" ")
                ):
                    # TNC-2 configuration commands
                    subcommands_meta = {
                        "display": "Show all TNC parameters",
                        "mycall": "Set your callsign",
                        "myalias": "Set your alias",
                        "mylocation": "Set Maidenhead grid square",
                        "connect": "Connect to station",
                        "disconnect": "Disconnect current connection",
                        "conv": "Enter conversation mode",
                        "unproto": "Set unproto destination",
                        "monitor": "Enable/disable packet monitoring",
                        "auto_ack": "Enable/disable auto ACK",
                        "retry": "Set retry count",
                        "retry_fast": "Set fast retry timeout",
                        "retry_slow": "Set slow retry timeout",
                        "digipeater": "Enable/disable digipeater",
                        "debug_buffer": "Set debug buffer size",
                        "status": "Show TNC status",
                        "reset": "Reset TNC settings",
                        "hardreset": "Hard reset (factory defaults)",
                        "powercycle": "Power cycle radio",
                        "tncsend": "Send raw hex to TNC",
                    }
                    word = words[1] if len(words) == 2 else ""
                    for subcmd, meta in subcommands_meta.items():
                        if subcmd.startswith(word.lower()):
                            yield Completion(
                                subcmd,
                                start_position=-len(word),
                                display=subcmd,
                                display_meta=meta,
                            )

    def _get_command_help(self, cmd):
        """Get brief help text for a command.

        Args:
            cmd: Command name

        Returns:
            Brief help string
        """
        help_text = {
            "help": "Show available commands",
            "status": "Show radio status",
            "health": "Show radio health",
            "notifications": "Toggle notifications",
            "vfo": "Select VFO (A/B)",
            "setvfo": "Set VFO frequency",
            "active": "Set active channel",
            "dual": "Toggle dual watch",
            "scan": "Toggle scan mode",
            "squelch": "Set squelch level",
            "volume": "Set volume level",
            "bss": "Show BSS status",
            "setbss": "Set BSS user ID",
            "poweron": "Power on radio",
            "poweroff": "Power off radio",
            "power": "Set TX power",
            "channel": "Show channel info",
            "list": "List channels",
            "freq": "Show/set frequency",
            "dump": "Dump config/status",
            "debug": "Set debug level (0-6), filter by station, or dump frames (dump/filter)",
            "tncsend": "Send TNC data",
            "aprs": "APRS commands / Switch to APRS mode",
            "radio": "Radio commands / Switch to radio mode",
            "scan_ble": "Scan BLE characteristics",
            "tnc": "Enter TNC mode",
            "quit": "Exit console",
            "exit": "Exit console",
            # APRS subcommands (when shown as top-level in APRS mode)
            "message": "APRS messaging",
            "msg": "APRS messaging (alias for message)",
            "station": "Station database",
            "wx": "Weather stations",
            "weather": "Weather stations (alias for wx)",
            "pws": "Personal Weather Station",
        }
        return help_text.get(cmd, "")
