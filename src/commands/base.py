"""
Base command handler with decorator-based registration.

Provides infrastructure for self-documenting commands with automatic
help text generation and tab completion support.
"""

from typing import Callable, List, Optional, Dict, Any
import inspect


def command(*names, help_text: str = "", usage: str = "", category: str = "general"):
    """
    Decorator to register command handler methods.

    Args:
        *names: Command names/aliases (e.g., "CONNECT", "C")
        help_text: Short help description (or use function docstring)
        usage: Usage syntax (e.g., "CONNECT <callsign> [via <path>]")
        category: Command category for grouping in help

    Example:
        @command("BEACON", "B", help_text="Control GPS beacons", category="aprs")
        async def beacon(self, args):
            '''Configure GPS position beaconing'''
            # Handler implementation
    """
    def decorator(func: Callable) -> Callable:
        # Use provided help_text or extract from docstring
        help_desc = help_text or (func.__doc__.strip() if func.__doc__ else "")

        # Store metadata on the function
        func._command_names = [n.upper() for n in names]
        func._command_help = help_desc
        func._command_usage = usage
        func._command_category = category
        func._is_command = True

        return func
    return decorator


class CommandHandler:
    """
    Base class for command handlers with automatic registration.

    Commands are registered via the @command decorator. The handler
    automatically builds command dispatch tables and provides introspection
    for help text and tab completion.
    """

    def __init__(self):
        self.commands: Dict[str, Dict[str, Any]] = {}
        self._register_commands()

    def _register_commands(self):
        """Scan class methods and register decorated commands."""
        for name in dir(self):
            if name.startswith('_'):
                continue

            method = getattr(self, name)
            if not callable(method):
                continue

            # Check if method is decorated as a command
            if hasattr(method, '_is_command'):
                for cmd_name in method._command_names:
                    self.commands[cmd_name] = {
                        'handler': method,
                        'help': method._command_help,
                        'usage': method._command_usage,
                        'category': method._command_category,
                        'method_name': name
                    }

    async def dispatch(self, cmd: str, args: List[str]) -> bool:
        """
        Dispatch command to registered handler.

        Args:
            cmd: Command name
            args: Command arguments

        Returns:
            True if command was found and executed, False otherwise
        """
        cmd_upper = cmd.upper()

        if cmd_upper not in self.commands:
            return False

        handler = self.commands[cmd_upper]['handler']

        # Call handler (supports both sync and async)
        if inspect.iscoroutinefunction(handler):
            await handler(args)
        else:
            handler(args)

        return True

    def get_command_names(self) -> List[str]:
        """Get list of all registered command names."""
        return sorted(self.commands.keys())

    def get_commands_by_category(self) -> Dict[str, List[str]]:
        """Group commands by category for help display."""
        categories: Dict[str, List[str]] = {}

        # Track primary names (not aliases)
        seen_methods = set()

        for cmd_name, cmd_info in sorted(self.commands.items()):
            method_name = cmd_info['method_name']

            # Skip if we've already added this method (it's an alias)
            if method_name in seen_methods:
                continue

            category = cmd_info['category']
            if category not in categories:
                categories[category] = []

            # Get all aliases for this command
            aliases = [n for n, i in self.commands.items()
                       if i['method_name'] == method_name]

            # Format: "PRIMARY (alias1, alias2)"
            if len(aliases) > 1:
                primary = aliases[0]
                others = ', '.join(aliases[1:])
                display = f"{primary} ({others})"
            else:
                display = aliases[0]

            categories[category].append(display)
            seen_methods.add(method_name)

        return categories

    def get_help(self, cmd: Optional[str] = None) -> str:
        """
        Get help text for a specific command or all commands.

        Args:
            cmd: Command name, or None for general help

        Returns:
            Formatted help text
        """
        if cmd:
            cmd_upper = cmd.upper()
            if cmd_upper not in self.commands:
                return f"Unknown command: {cmd}"

            info = self.commands[cmd_upper]
            help_lines = []

            # Get all aliases
            aliases = [n for n, i in self.commands.items()
                       if i['method_name'] == info['method_name']]

            if len(aliases) > 1:
                help_lines.append(f"Command: {', '.join(aliases)}")
            else:
                help_lines.append(f"Command: {aliases[0]}")

            if info['help']:
                help_lines.append(f"Description: {info['help']}")

            if info['usage']:
                help_lines.append(f"Usage: {info['usage']}")

            return '\n'.join(help_lines)
        else:
            # General help - list all commands by category
            help_lines = []
            categories = self.get_commands_by_category()

            for category, commands in sorted(categories.items()):
                help_lines.append(f"\n{category.upper()} Commands:")
                for cmd_display in commands:
                    # Get the primary command name for help text
                    primary = cmd_display.split()[0]
                    if primary in self.commands:
                        help_text = self.commands[primary]['help']
                        help_lines.append(f"  {cmd_display:20s} {help_text}")

            return '\n'.join(help_lines)

    def get_completions(self, text: str) -> List[str]:
        """
        Get command completions for tab completion.

        Args:
            text: Partial command text

        Returns:
            List of matching command names
        """
        text_upper = text.upper()
        return [cmd for cmd in self.commands.keys()
                if cmd.startswith(text_upper)]
