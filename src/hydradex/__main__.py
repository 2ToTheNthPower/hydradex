"""Command-line entry point; stdout is reserved for the LSP transport."""

import argparse
import logging

from hydradex import __version__


def main() -> None:
    parser = argparse.ArgumentParser(description="Hydra YAML language server")
    parser.add_argument("--version", action="version", version=f"hydradex {__version__}")
    parser.add_argument("--stdio", action="store_true", help="Use stdio (the default)")
    parser.add_argument(
        "--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="WARNING"
    )
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level)
    from hydradex.server import create_server

    server = create_server()
    try:
        server.start_io()
    finally:
        server.cleanup()


if __name__ == "__main__":
    main()
