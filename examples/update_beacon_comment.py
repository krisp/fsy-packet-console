#!/usr/bin/env python3
"""
Example script to update beacon comment via HTTP POST API.

Usage:
    python examples/update_beacon_comment.py
"""

import requests
import json
import sys

# Configuration
WEBUI_URL = "http://localhost:10002"  # Default web UI address
PASSWORD = "your_password_here"  # Set in TNC config WEBUI_PASSWORD


def update_beacon_comment(comment, tx=False, quiet=False):
    """
    Update the beacon comment via POST API.

    Args:
        comment: New beacon comment text (max 43 chars, will be truncated)
        tx: If True, send beacon immediately
        quiet: If True, return minimal response

    Returns:
        Response JSON dict
    """
    url = f"{WEBUI_URL}/api/beacon/comment"

    payload = {
        "password": PASSWORD,
        "comment": comment,
        "tx": tx,
        "quiet": quiet
    }

    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error: {e}")
        if hasattr(e, 'response') and e.response is not None:
            try:
                print(f"Server response: {e.response.json()}")
            except:
                print(f"Server response: {e.response.text}")
        sys.exit(1)


def main():
    """Main function demonstrating various usage scenarios."""

    print("=" * 60)
    print("Beacon Comment Update Examples")
    print("=" * 60)

    # Example 1: Basic update
    print("\n1. Basic update (no immediate transmission):")
    result = update_beacon_comment("Testing API update")
    print(f"   Success: {result['success']}")
    print(f"   Comment: {result['comment']}")
    print(f"   Truncated: {result['truncated']}")
    print(f"   Beacon Status: {result['beacon_status']['enabled']}")

    # Example 2: Update with immediate transmission
    print("\n2. Update with immediate beacon transmission:")
    result = update_beacon_comment("Live from API!", tx=True)
    print(f"   Success: {result['success']}")
    print(f"   Comment: {result['comment']}")
    print(f"   Beacon Sent: {result['beacon_sent']}")

    # Example 3: Quiet mode (minimal response)
    print("\n3. Quiet mode update:")
    result = update_beacon_comment("Quiet update", quiet=True)
    print(f"   Success: {result['success']}")
    print(f"   Comment: {result['comment']}")
    print(f"   (No beacon_status in quiet mode)")

    # Example 4: Long comment (will be truncated)
    print("\n4. Long comment (>43 chars, will be truncated):")
    long_comment = "This is a very long comment that exceeds the 43 character APRS limit"
    result = update_beacon_comment(long_comment)
    print(f"   Original: {long_comment}")
    print(f"   Truncated: {result['comment']}")
    print(f"   Length: {len(result['comment'])} chars")
    print(f"   Was truncated: {result['truncated']}")

    print("\n" + "=" * 60)
    print("All examples completed successfully!")
    print("=" * 60)


if __name__ == "__main__":
    # Check if password is still default
    if PASSWORD == "your_password_here":
        print("ERROR: Please set PASSWORD in this script first!")
        print("The password must match WEBUI_PASSWORD in your TNC config.")
        print("\nTo set the password, run this console command:")
        print("  WEBUI_PASSWORD your_secure_password")
        sys.exit(1)

    main()
