"""Absolute imports also work when PyInstaller executes this as a script."""

from fetchlane.cli import main

raise SystemExit(main())
