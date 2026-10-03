"""Command line entry point for shared monitoring tools."""
import argparse
from . import __version__
from .config import ConfigError, load_config


def main(argv=None):
    parser = argparse.ArgumentParser(prog='connection-monitoring')
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('--config')
    parser.add_argument('command', choices=['validate', 'notifier'])
    args = parser.parse_args(argv)
    try:
        if not args.config:
            raise ConfigError('--config is required for ' + args.command)
        config = load_config(args.config, section='notifier' if args.command == 'notifier' else None)
        if 'notifier' in config:
            from .notifier_config import validate_runtime_config
            validate_runtime_config(config['notifier'])
        if args.command == 'notifier':
            from .alert_server import main as notifier_main
            notifier_main(config['notifier'])
    except ConfigError as error:
        parser.error(str(error))
    return 0
