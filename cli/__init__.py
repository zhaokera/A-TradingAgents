"""Command-line interfaces.

Package import must stay side-effect free so JSON automation commands and
``--help`` do not initialize LLM or database storage.
"""

import logging


logger = logging.getLogger("cli")
