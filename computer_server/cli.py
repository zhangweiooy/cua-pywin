"""
Command-line interface for the Computer API server.
"""

import argparse
import logging
import os
import sys
from typing import List, Optional

logger = logging.getLogger(__name__)


def parse_args(args: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Start the Computer API server")
    parser.add_argument(
        "--width",
        type=int,
        help="Target width for screenshots (coordinates will be scaled accordingly)",
    )
    parser.add_argument(
        "--height",
        type=int,
        help="Target height for screenshots (coordinates will be scaled accordingly)",
    )
    parser.add_argument(
        "--detect-resolution",
        action="store_true",
        help="Auto-detect and log the actual screen resolution at startup",
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="Host to bind the server to (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="Port to bind the server to (default: 8000)"
    )
    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error", "critical"],
        default="info",
        help="Logging level (default: info)",
    )
    parser.add_argument(
        "--ssl-keyfile",
        type=str,
        help="Path to SSL private key file (enables HTTPS)",
    )
    parser.add_argument(
        "--ssl-certfile",
        type=str,
        help="Path to SSL certificate file (enables HTTPS)",
    )
    return parser.parse_args(args)


def main() -> None:
    """Main entry point for the CLI."""
    args = parse_args()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr,  # Use stderr for MCP compatibility
    )

    # Create and start the server
    logger.info(f"Starting Cua Computer API server on {args.host}:{args.port}...")
    logger.info("HTTP API available at /ws, /status endpoints")
    logger.info("MCP server available at /mcp endpoint (if fastmcp installed)")

    # Handle SSL configuration
    ssl_args = {}
    if args.ssl_keyfile and args.ssl_certfile:
        ssl_args = {
            "ssl_keyfile": args.ssl_keyfile,
            "ssl_certfile": args.ssl_certfile,
        }
        logger.info("HTTPS mode enabled with SSL certificates")
    elif args.ssl_keyfile or args.ssl_certfile:
        logger.warning(
            "Both --ssl-keyfile and --ssl-certfile are required for HTTPS. Running in HTTP mode."
        )
    else:
        logger.info("HTTP mode (no SSL certificates provided)")

    if sys.platform != "win32":
        logger.error("cua-pywin 仅支持 Windows 平台")
        sys.exit(1)

    # Import Server lazily so the module-level handler factory runs after
    # the platform check.
    from .server import Server

    server = Server(host=args.host, port=args.port, log_level=args.log_level, **ssl_args)

    try:
        server.start()
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Error starting server: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
