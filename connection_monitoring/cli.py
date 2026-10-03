"""Command line entry point for shared monitoring tools."""
import argparse
from . import __version__
from .config import ConfigError, load_config


def main(argv=None):
    parser = argparse.ArgumentParser(prog='connection-monitoring')
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('--config', required=True)
    parser.add_argument('command', choices=['validate'])
    args = parser.parse_args(argv)
    try:
        load_config(args.config)
    except ConfigError as error:
        parser.error(str(error))
    return 0
