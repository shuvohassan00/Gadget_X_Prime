#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Compatibility launcher.

Some deployments run `python3 prime_bot.py`. Keep this entrypoint in sync with
the main bot implementation so runtime always uses the latest patched logic.
"""

from gadget_x_prime_ultra_complete_v8_premium_ui import run_bot


if __name__ == "__main__":
    run_bot()
