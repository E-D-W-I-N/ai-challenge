"""Compatibility alias for explicit legacy stdio fixtures and direct checks."""
import sys
from services.pipeline import server as implementation

if __name__ == "__main__":
    implementation.server.run()
else:
    # Preserve the existing fixture's module-level patch points without
    # duplicating algorithms or importing Agent, Store, or LLM in the service.
    sys.modules[__name__] = implementation
