import os
import sys

from .app import main

sys.exit(main(os.environ.get("SYNAPTIX_LOCALEDIR")))
