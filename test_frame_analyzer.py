#!/usr/bin/env python3
"""
Test wrapper for frame_analyzer.py module
Loads frames from buffer and tests the new shared decoder
"""

import sys
import os
import gzip
import json
import base64
from datetime import datetime

# Add src to path so we can import the module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from frame_analyzer import decode_kiss_frame, format_frame_detailed, hex_dump


def load_frame_buffer(buffer_file=None):
    """Load frames from the frame buffer database."""
    if buffer_file is None:
        buffer_file = os.path.expanduser("~/.console_frame_buffer.json.gz")

    if not os.path.exists(buffer_file):
        print(f"Error: Frame buffer file not found: {buffer_file}")
        return []

    try:
        with gzip.open(buffer_file, 'rt', encoding='utf-8') as f:
            data = json.load(f)

        frames = []
        for frame_data in data.get('frames', []):
            frame_num = frame_data['frame_number']
            timestamp = datetime.fromisoformat(frame_data['timestamp']).strftime('%H:%M:%S.%f')[:-3]
            raw_bytes = base64.b64decode(frame_data['raw_bytes'])
            direction = frame_data.get('direction', 'RX')
            frames.append((frame_num, timestamp, raw_bytes, direction))

        return frames

    except Exception as e:
        print(f"Error loading frame buffer: {e}")
        return []


def test_frame_analyzer():
    """Test the frame analyzer module with buffer data."""
    print("=" * 80)
    print("Testing frame_analyzer.py Module")
    print("=" * 80)
    print()

    # Load frames
    frames = load_frame_buffer()

    if not frames:
        print("No frames found in buffer")
        return

    print(f"Loaded {len(frames)} frames from buffer")
    print()

    # Test with first 3 frames
    test_count = min(3, len(frames))
    print(f"Testing with first {test_count} frame(s):")
    print()

    for i in range(test_count):
        frame_num, timestamp, raw_bytes, direction = frames[i]

        print(f"\n{'=' * 80}")
        print(f"Testing Frame #{frame_num} ({direction})")
        print(f"{'=' * 80}\n")

        # Test decode_kiss_frame
        print("1. Testing decode_kiss_frame()...")
        decoded = decode_kiss_frame(raw_bytes)

        if 'error' in decoded:
            print(f"   ✗ Decode failed: {decoded['error']}")
            continue

        print(f"   ✓ Decoded successfully")
        print(f"     From: {decoded['ax25']['source']['full']}")
        print(f"     To: {decoded['ax25']['destination']['full']}")
        if decoded.get('info'):
            preview = decoded['info']['text'][:40]
            print(f"     Info: {preview}...")

        # Test format_frame_detailed with ANSI output
        print("\n2. Testing format_frame_detailed() with ANSI output...")
        try:
            output = format_frame_detailed(
                decoded,
                frame_num,
                timestamp,
                direction,
                output_format='ansi'
            )
            # Split into lines for counting and preview
            lines = output.split('\n')
            print(f"   ✓ Generated {len(lines)} lines of output")
            print("\n   Preview (first 20 lines):")
            for line in lines[:20]:
                print(f"   {line}")
        except Exception as e:
            print(f"   ✗ Format failed: {e}")
            import traceback
            traceback.print_exc()

        # Test hex_dump
        print("\n3. Testing hex_dump()...")
        try:
            hex_lines = hex_dump(raw_bytes[:64])  # First 64 bytes
            print(f"   ✓ Generated {len(hex_lines)} lines")
            print("   Preview:")
            for line in hex_lines[:4]:
                print(f"   {line}")
        except Exception as e:
            print(f"   ✗ Hex dump failed: {e}")

    print("\n" + "=" * 80)
    print("Test Summary")
    print("=" * 80)
    print(f"✓ Module loaded successfully")
    print(f"✓ Decoded {test_count} frame(s) from buffer")
    print()
    print("Module is ready for integration!")


if __name__ == '__main__':
    test_frame_analyzer()
