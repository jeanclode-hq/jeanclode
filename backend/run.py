"""Main entry point for Jeanclode application."""

import asyncio
import logging
import os

from api.app import Application

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logging.getLogger("faststream").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


async def main():
    """Run the application."""
    config_path = os.environ.get("JEANCLODE_CONFIG")
    logger.info(f"Loading configuration from: {config_path}")
    if not config_path:
        raise ValueError("JEANCLODE_CONFIG environment variable not set")
    app = await Application.from_config(config_path)
    await app.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        raise
