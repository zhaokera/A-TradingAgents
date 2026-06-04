"""Compatibility wrapper for the holdings CLI subcommand."""

from app.services.holdings_cli import (
    CLIError,
    build_holdings_payload,
    build_summary_payload,
    build_users_payload,
    holdings_app,
    main,
    select_user,
)

__all__ = [
    "CLIError",
    "build_holdings_payload",
    "build_summary_payload",
    "build_users_payload",
    "holdings_app",
    "main",
    "select_user",
]


if __name__ == "__main__":
    main()
