"""Clean JSON entrypoint for local holdings data.

Use this module for automation:
    python -m holdings_cli list
"""

from app.services.holdings_cli import main


if __name__ == "__main__":
    main()
