import sys
import os

# Append the src/ directory to the path so the compliance_agent package is discoverable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "src")))

from compliance_agent.auto_healer import main

if __name__ == "__main__":
    sys.exit(main())
