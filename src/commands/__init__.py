"""
Command handler infrastructure for FSY Packet Console.

This package provides a decorator-based command registration system that
eliminates boilerplate and enables automatic help generation and tab completion.
"""

from .base import CommandHandler, command

__all__ = ['CommandHandler', 'command']
