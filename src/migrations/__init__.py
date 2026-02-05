"""Database migration system for APRS console.

Migrations are one-time operations that transform existing database data
to match new schema or fix data quality issues. Each migration runs exactly
once and is tracked in the database state.

Migration Files:
----------------
Each migration lives in its own file following the naming pattern:
    m###_descriptive_name.py

Where ### is a zero-padded sequential number (001, 002, etc.)

Each migration file must have a migrate(aprs_manager, console) function
that returns a dict with statistics.

Example:
--------
src/migrations/
    __init__.py              # This file - MigrationManager
    m001_zero_hop_counts.py  # First migration
    m002_fix_timestamps.py   # Second migration
    README.md                # Documentation

Adding a New Migration:
-----------------------
1. Create new file: m###_description.py (increment ### from last migration)
2. Add docstring explaining problem, solution, impact
3. Implement migrate(aprs_manager, console) -> Dict[str, Any]
4. Test thoroughly - migrations can't be undone easily
5. Commit with descriptive message

Migration runs automatically on console startup.
"""

import os
import importlib
import logging
from typing import Dict, Any, List, Tuple, Callable
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def discover_migrations() -> List[Tuple[str, Callable, str]]:
    """Automatically discover all migration files in this directory.

    Returns:
        List of tuples: (migration_id, migrate_function, description)
        Sorted by migration number (m001, m002, etc.)
    """
    migrations = []
    migrations_dir = os.path.dirname(__file__)

    # Find all m###_*.py files
    for filename in sorted(os.listdir(migrations_dir)):
        if not filename.startswith('m') or not filename.endswith('.py'):
            continue
        if filename == '__init__.py':
            continue

        # Extract migration ID from filename (e.g., "m001_zero_hop_counts" from "m001_zero_hop_counts.py")
        migration_id = filename[:-3]  # Remove .py extension

        try:
            # Import the migration module
            module_name = f"src.migrations.{migration_id}"
            module = importlib.import_module(module_name)

            # Get the migrate function
            if not hasattr(module, 'migrate'):
                logger.warning(f"Migration {migration_id} missing migrate() function, skipping")
                continue

            migrate_func = module.migrate

            # Get description from module docstring
            description = module.__doc__.split('\n')[0] if module.__doc__ else migration_id

            migrations.append((migration_id, migrate_func, description))

        except Exception as e:
            logger.error(f"Failed to load migration {migration_id}: {e}", exc_info=True)
            continue

    return migrations


class MigrationManager:
    """Manages database migrations."""

    def __init__(self, aprs_manager, console):
        """Initialize migration manager.

        Args:
            aprs_manager: APRSManager instance
            console: CommandProcessor instance
        """
        self.aprs_manager = aprs_manager
        self.console = console
        self.migration_state = {}  # Loaded from database

    def load_migration_state(self):
        """Load migration state from database.

        Migration state is stored in the database as a dict:
        {
            'migrations_applied': {
                'migration_id': {
                    'timestamp': ISO timestamp,
                    'result': stats dict
                }
            }
        }
        """
        # Load migration state from APRSManager
        self.migration_state = self.aprs_manager.migrations

        if 'migrations_applied' not in self.migration_state:
            self.migration_state['migrations_applied'] = {}

    def save_migration_state(self):
        """Save migration state to database."""
        self.aprs_manager.migrations = self.migration_state
        self.aprs_manager.save_database()

    def has_migration_run(self, migration_id: str) -> bool:
        """Check if a migration has already been applied.

        Args:
            migration_id: Unique migration identifier (e.g., "m001_zero_hop_counts")

        Returns:
            True if migration was already applied
        """
        return migration_id in self.migration_state.get('migrations_applied', {})

    def run_migrations(self, force: bool = False) -> Dict[str, Any]:
        """Run all pending migrations.

        Automatically discovers and runs migrations in order (m001, m002, m003, ...).

        Args:
            force: If True, re-run all migrations (for testing only)

        Returns:
            Dict with overall migration results:
            {
                'total': int,         # Total migrations discovered
                'applied': int,       # Successfully applied
                'skipped': int,       # Already applied
                'failed': int,        # Failed to apply
                'details': dict       # Per-migration results
            }
        """
        self.load_migration_state()

        # Auto-discover migrations
        migrations = discover_migrations()

        results = {
            'total': len(migrations),
            'applied': 0,
            'skipped': 0,
            'failed': 0,
            'details': {}
        }

        for migration_id, migration_func, description in migrations:
            # Skip if already applied (unless force=True)
            if not force and self.has_migration_run(migration_id):
                results['skipped'] += 1
                results['details'][migration_id] = {
                    'status': 'skipped',
                    'reason': 'Already applied'
                }
                continue

            try:
                logger.info(f"Running migration: {migration_id} - {description}")

                # Run the migration
                migration_result = migration_func(self.aprs_manager, self.console)

                # Record success
                self.migration_state['migrations_applied'][migration_id] = {
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                    'result': migration_result
                }

                results['applied'] += 1
                results['details'][migration_id] = {
                    'status': 'applied',
                    'description': description,
                    'result': migration_result
                }

                logger.info(f"Migration {migration_id} completed: {migration_result}")

            except Exception as e:
                logger.error(f"Migration {migration_id} failed: {e}", exc_info=True)
                results['failed'] += 1
                results['details'][migration_id] = {
                    'status': 'failed',
                    'error': str(e)
                }

        # Save state after all migrations
        if results['applied'] > 0:
            self.save_migration_state()

        return results


def run_startup_migrations(aprs_manager, console, quiet: bool = False) -> Dict[str, Any]:
    """Run database migrations on startup.

    This is the main entry point called from console.py during initialization.

    Args:
        aprs_manager: APRSManager instance
        console: CommandProcessor instance
        quiet: If True, don't print messages (for testing)

    Returns:
        Migration results dict, or None if disabled
    """
    # Check if migrations are disabled in config
    if hasattr(console, 'tnc_config'):
        if console.tnc_config.get('DISABLE_MIGRATIONS') == 'true':
            if not quiet:
                logger.info("Database migrations disabled by config")
            return None

    manager = MigrationManager(aprs_manager, console)
    results = manager.run_migrations()

    # Report results (if not quiet)
    if not quiet and results['applied'] > 0:
        from src.utils import print_info, print_header

        print_header("Database Migrations")
        print_info(f"Applied {results['applied']} migration(s)")

        for migration_id, details in results['details'].items():
            if details['status'] == 'applied':
                result = details.get('result', {})
                print_info(f"  ✓ {details['description']}")

                if isinstance(result, dict) and result:
                    # Migration #001 format: {migrated: N, total_packets: N, candidates: N}
                    if 'migrated' in result:
                        migrated = result.get('migrated', 0)
                        if migrated > 0:
                            print_info(f"    Migrated {migrated} station(s), "
                                     f"{result.get('total_packets', 0)} packets")
                        elif result.get('candidates', 0) > 0:
                            print_info(f"    Found {result['candidates']} candidate(s), "
                                     f"but no packets in frame buffer")
                    # Migration #002 format: {cleared: N, stations: [...]}
                    elif 'cleared' in result:
                        cleared = result.get('cleared', 0)
                        if cleared > 0:
                            print_info(f"    Cleared {cleared} station(s)")
                        else:
                            print_info(f"    No stations needed clearing")
                    # Skip message format: {skipped: 'reason'}
                    elif 'skipped' in result:
                        print_info(f"    {result['skipped']}")

    return results
