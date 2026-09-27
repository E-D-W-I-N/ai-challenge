"""Legacy stdio entry point; the service itself lives outside the app."""
import sys
from services.echo.__main__ import main

if __name__ == "__main__":
    sys.argv.append("--stdio")
    main()
