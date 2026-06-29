import sys
import os
import asyncio

# Append the src/ directory to the path so the compliance_agent package is discoverable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "src")))

from compliance_agent.main import async_main

if __name__ == "__main__":
    sys.exit(asyncio.run(async_main()))
