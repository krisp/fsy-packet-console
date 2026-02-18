"""Frame history tracking for debugging."""

import asyncio
import base64
import gzip
import json
import os
import time
import traceback
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List

try:
    import ujson
    HAS_UJSON = True
except ImportError:
    HAS_UJSON = False

from src.utils import (
    print_debug,
    print_error,
    print_info,
    print_warning,
)

@dataclass
class FrameHistoryEntry:
    """Represents a captured frame for debugging."""

    timestamp: datetime
    direction: str  # 'RX' or 'TX'
    raw_bytes: bytes
    frame_number: int  # Sequential frame number

    def format_hex(self) -> str:
        """Format frame as hex dump."""
        hex_str = self.raw_bytes.hex()
        # Format as groups of 2 hex chars (bytes)
        formatted = " ".join(
            hex_str[i : i + 2] for i in range(0, len(hex_str), 2)
        )
        return formatted

    def format_ascii(self, chunk: bytes) -> str:
        """Format bytes as ASCII (printable chars or dots)."""
        return "".join(chr(b) if 0x20 <= b <= 0x7E else "." for b in chunk)

    def format_hex_lines(self) -> List[str]:
        """Format frame as hex editor style lines (hex + ASCII)."""
        lines = []
        for i in range(0, len(self.raw_bytes), 16):
            chunk = self.raw_bytes[i : i + 16]
            hex_part = " ".join(f"{b:02x}" for b in chunk)
            # Pad hex part to align ASCII column (16 bytes * 3 chars - 1 space = 47 chars)
            hex_part = hex_part.ljust(47)
            ascii_part = self.format_ascii(chunk)
            lines.append(f"  {hex_part}  {ascii_part}")
        return lines


class FrameHistory:
    """Tracks recent frames for debugging."""

    # File path for persistent storage
    BUFFER_FILE = os.path.expanduser("~/.console_frame_buffer.json.gz")
    AUTO_SAVE_INTERVAL = 100  # Save every N frames

    def __init__(self, max_size_mb: int = 10, buffer_mode: bool = True):
        self.buffer_mode = buffer_mode  # True = MB-based, False = simple 10-frame buffer
        self.frame_counter = 0  # Global frame counter (never resets)
        self.frames_since_save = 0  # Track frames added since last save

        if buffer_mode:
            self.frames = deque()  # No maxlen
            self.max_size_bytes = max_size_mb * 1024 * 1024  # Convert MB to bytes
            self.current_size_bytes = 0
        else:
            # Simple mode: just keep last 10 frames
            self.frames = deque(maxlen=10)
            self.max_size_bytes = 0
            self.current_size_bytes = 0

        # Async save lock to prevent concurrent saves
        self._save_lock = asyncio.Lock()
        self._last_save_time = 0  # Track last save for monitoring

        # Note: load_from_disk() called explicitly after creation to display load info

    def add_frame(self, direction: str, raw_bytes: bytes):
        """Add a frame to history.

        Args:
            direction: 'RX' or 'TX'
            raw_bytes: Complete KISS frame bytes
        """
        self.frame_counter += 1
        entry = FrameHistoryEntry(
            timestamp=datetime.now().astimezone(),  # Timezone-aware in local timezone
            direction=direction,
            raw_bytes=raw_bytes,
            frame_number=self.frame_counter
        )
        self.frames.append(entry)

        if self.buffer_mode:
            self.current_size_bytes += len(raw_bytes)
            # Remove old frames if we exceed size limit
            while self.current_size_bytes > self.max_size_bytes and len(self.frames) > 1:
                removed = self.frames.popleft()
                self.current_size_bytes -= len(removed.raw_bytes)

        # Auto-save periodically (async to avoid blocking)
        self.frames_since_save += 1
        if self.frames_since_save >= self.AUTO_SAVE_INTERVAL:
            # Trigger async save without blocking
            asyncio.create_task(self.save_to_disk_async())
            self.frames_since_save = 0

    def get_recent(self, count: int = None) -> List[FrameHistoryEntry]:
        """Get recent frames.

        Args:
            count: Number of frames to return (None = all)

        Returns:
            List of frames (most recent last)
        """
        if count is None:
            return list(self.frames)
        else:
            # Return last N frames
            return list(self.frames)[-count:]

    def get_by_number(self, frame_number: int) -> FrameHistoryEntry:
        """Get a specific frame by its number.

        Args:
            frame_number: Frame number to retrieve

        Returns:
            FrameHistoryEntry or None if not found
        """
        for frame in self.frames:
            if frame.frame_number == frame_number:
                return frame
        return None

    def set_buffer_mode(self, buffer_mode: bool, size_mb: int = 10):
        """Switch between buffer modes.

        Args:
            buffer_mode: True = MB-based, False = simple 10-frame
            size_mb: Size in MB for buffer mode
        """
        self.buffer_mode = buffer_mode

        if buffer_mode:
            # Convert to MB-based mode
            self.max_size_bytes = size_mb * 1024 * 1024
            # Recreate deque without maxlen
            old_frames = list(self.frames)
            self.frames = deque()
            for frame in old_frames:
                self.frames.append(frame)
            # Calculate current size
            self.current_size_bytes = sum(len(f.raw_bytes) for f in self.frames)
            # Trim if needed
            while self.current_size_bytes > self.max_size_bytes and len(self.frames) > 1:
                removed = self.frames.popleft()
                self.current_size_bytes -= len(removed.raw_bytes)
        else:
            # Convert to simple mode
            old_frames = list(self.frames)[-10:]  # Keep last 10
            self.frames = deque(old_frames, maxlen=10)
            self.max_size_bytes = 0
            self.current_size_bytes = 0

    def set_max_size_mb(self, size_mb: int):
        """Change buffer size limit.

        Args:
            size_mb: New size in MB
        """
        if self.buffer_mode:
            self.max_size_bytes = size_mb * 1024 * 1024
            # Trim if needed
            while self.current_size_bytes > self.max_size_bytes and len(self.frames) > 1:
                removed = self.frames.popleft()
                self.current_size_bytes -= len(removed.raw_bytes)

    async def save_to_disk_async(self):
        """Save frame buffer to disk asynchronously (non-blocking).

        Uses asyncio.to_thread to run the blocking save operation in a thread pool,
        preventing event loop blocking. Includes lock to prevent concurrent saves.
        """
        # Prevent concurrent saves
        if self._save_lock.locked():
            print_debug("Frame buffer save already in progress, skipping", level=3)
            return

        async with self._save_lock:
            save_start = time.time()
            try:
                # Run blocking save in thread pool
                await asyncio.to_thread(self.save_to_disk)
                save_duration = time.time() - save_start
                self._last_save_time = time.time()
                print_debug(f"Frame buffer saved asynchronously in {save_duration:.2f}s", level=3)
            except Exception as e:
                print_error(f"Async frame buffer save failed: {e}")

    def save_to_disk(self):
        """Save frame buffer to disk (compressed JSON format).

        Saves frames to ~/.console_frame_buffer.json.gz for persistence
        across restarts.

        Note: This is a blocking operation. Use save_to_disk_async() for non-blocking saves.
        """
        try:
            # Take a snapshot of frames to avoid "deque mutated during iteration" error
            # (frames may be added by background tasks while we're saving)
            frames_snapshot = list(self.frames)

            # Serialize frames to JSON-compatible format
            frames_data = []
            for frame in frames_snapshot:
                frames_data.append({
                    'timestamp': frame.timestamp.isoformat(),
                    'direction': frame.direction,
                    'raw_bytes': base64.b64encode(frame.raw_bytes).decode('ascii'),
                    'frame_number': frame.frame_number
                })

            data = {
                'frame_counter': self.frame_counter,
                'buffer_mode': self.buffer_mode,
                'max_size_mb': self.max_size_bytes // (1024 * 1024) if self.buffer_mode else 0,
                'frames': frames_data,
                'saved_at': datetime.now(timezone.utc).isoformat()
            }

            # Write compressed JSON
            temp_file = self.BUFFER_FILE + ".tmp"
            with gzip.open(temp_file, 'wt', encoding='utf-8') as f:
                # Use ujson for 3-5x faster serialization if available
                if HAS_UJSON:
                    f.write(ujson.dumps(data, ensure_ascii=False))
                else:
                    json.dump(data, f)

            # Atomic rename
            os.replace(temp_file, self.BUFFER_FILE)

        except Exception as e:
            # Log error but don't disrupt operation
            print_error(f"Failed to save frame buffer: {type(e).__name__}: {e}")
            print_debug(traceback.format_exc(), level=3)

    async def load_from_disk_async(self) -> dict:
        """Load frame buffer from disk asynchronously (non-blocking).

        Uses asyncio.to_thread to run the blocking load operation in a thread pool,
        allowing parallel loading with other startup tasks.

        Returns:
            dict with keys: loaded (bool), frame_count (int), start_frame (int),
            file_size_kb (float), corrupted_frames (int)
        """
        return await asyncio.to_thread(self.load_from_disk)

    def load_from_disk(self) -> dict:
        """Load frame buffer from disk if available (blocking).

        Restores frames from ~/.console_frame_buffer.json.gz to maintain
        debugging history across restarts.

        Note: For async loading during startup, use load_from_disk_async().

        Returns:
            dict with keys: loaded (bool), frame_count (int), start_frame (int),
            file_size_kb (float), corrupted_frames (int)
        """
        result = {
            'loaded': False,
            'frame_count': 0,
            'start_frame': 0,
            'file_size_kb': 0.0,
            'corrupted_frames': 0
        }

        if not os.path.exists(self.BUFFER_FILE):
            return result

        try:
            # Get file size before loading
            result['file_size_kb'] = os.path.getsize(self.BUFFER_FILE) / 1024

            with gzip.open(self.BUFFER_FILE, 'rt', encoding='utf-8') as f:
                # Use ujson for faster deserialization if available
                if HAS_UJSON:
                    data = ujson.loads(f.read())
                else:
                    data = json.load(f)

            # Restore frame counter (important to maintain sequential numbering)
            self.frame_counter = data.get('frame_counter', 0)

            # Pre-compute local timezone once (optimization for legacy naive timestamps)
            local_tz = datetime.now(timezone.utc).astimezone().tzinfo

            # Restore frames
            corrupted = 0
            for frame_data in data.get('frames', []):
                try:
                    # Load timestamp and make timezone-aware if needed
                    ts = datetime.fromisoformat(frame_data['timestamp'])
                    if ts.tzinfo is None:
                        # Naive timestamp from old data - treat as local time, make aware
                        ts = ts.replace(tzinfo=local_tz)

                    entry = FrameHistoryEntry(
                        timestamp=ts,
                        direction=frame_data['direction'],
                        raw_bytes=base64.b64decode(frame_data['raw_bytes']),
                        frame_number=frame_data['frame_number']
                    )
                    self.frames.append(entry)

                    # Update size tracking for buffer mode
                    if self.buffer_mode:
                        self.current_size_bytes += len(entry.raw_bytes)

                except Exception:
                    # Skip corrupted frames but continue loading others
                    corrupted += 1
                    continue

            # Trim to current buffer size if needed
            trimmed = 0
            if self.buffer_mode:
                while self.current_size_bytes > self.max_size_bytes and len(self.frames) > 1:
                    removed = self.frames.popleft()
                    self.current_size_bytes -= len(removed.raw_bytes)
                    trimmed += 1

            # Calculate start frame number (lowest frame in buffer)
            start_frame = self.frames[0].frame_number if self.frames else self.frame_counter

            result['loaded'] = True
            result['frame_count'] = len(self.frames)
            result['start_frame'] = start_frame
            result['corrupted_frames'] = corrupted

            return result

        except Exception as e:
            # If load fails (corrupted file, format change, etc.), start fresh
            print_error(f"Failed to load frame buffer: {type(e).__name__}: {e}")
            print_warning("Starting with empty frame buffer")
            print_debug(traceback.format_exc(), level=3)
            self.frames.clear()
            self.frame_counter = 0
            self.current_size_bytes = 0
            return result

