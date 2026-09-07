"""Test doubles shared across the suite.

A package rather than a loose module so that `from fakes.scripted_adapter import ...`
reads the same from any test file, and so that a second double — an NSE-shaped adapter,
say — has somewhere obvious to go.
"""
