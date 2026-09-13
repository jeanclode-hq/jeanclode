"""Entry point for the jeanclode CLI."""

from __future__ import annotations

import asyncio
import logging
import sys

from src.cli import parse_args
from src.log import setup_logging

logger = logging.getLogger(__name__)


async def main(argv: list[str] | None = None) -> int:
    """Parse args and delegate to runner."""
    args = parse_args(argv)
    setup_logging(debug=args.debug)

    from src.runner import run

    return await run(args)


def cli_entry() -> None:
    """Entry point for the ``jeanclode`` console script."""
    try:
        code = asyncio.run(main())
        sys.exit(code)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        logger.exception("unexpected error")
        sys.exit(1)


if __name__ == "__main__":
    cli_entry()
