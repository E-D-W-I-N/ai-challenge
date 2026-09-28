"""Compatibility entry point for the independent reminder service over stdio."""
from services.reminders.server import *  # noqa: F403

if __name__ == "__main__":
    server.run()  # noqa: F405
