"""Run the pipeline steps in order against a local work directory."""

import argparse
from collections.abc import Sequence
import dataclasses
import logging
import os
from pathlib import Path
import sys
import threading
import time

from smithsonian_open_access_catalog import catalog, connection, listing, metadata, upload
from smithsonian_open_access_catalog.config import Config

logger = logging.getLogger(__name__)

STEPS = (
    ('list', listing.run),
    ('metadata', metadata.run),
    ('catalog', catalog.run),
    ('upload', upload.run),
)
STEP_NAMES = [name for name, _ in STEPS]

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_TIMEOUT = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from-step', choices=STEP_NAMES, default='list')
    parser.add_argument('--only-step', choices=STEP_NAMES)
    parser.add_argument('--skip-upload', action='store_true')
    parser.add_argument('--work-dir', type=Path, help='overrides CATALOG_WORK_DIR')
    return parser


def selected_steps(from_step: str, only_step: str | None, skip_upload: bool) -> list[str]:
    names = [only_step] if only_step else STEP_NAMES[STEP_NAMES.index(from_step) :]
    return [name for name in names if not (skip_upload and name == 'upload')]


def start_watchdog(minutes: int) -> None:
    def expire() -> None:
        time.sleep(minutes * 60)
        logger.error('Exceeded the %d-minute runtime limit; exiting', minutes)
        os._exit(EXIT_TIMEOUT)

    threading.Thread(target=expire, name='watchdog', daemon=True).start()


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )
    args = build_parser().parse_args(argv)
    config = Config.from_env()
    if args.work_dir is not None:
        config = dataclasses.replace(config, work_dir=args.work_dir)
    steps = selected_steps(args.from_step, args.only_step, args.skip_upload)
    logger.info('Steps: %s; work dir: %s', ', '.join(steps), config.work_dir)

    start_watchdog(config.max_runtime_minutes)
    try:
        conn = connection.connect(config)
        for name, step in STEPS:
            if name not in steps:
                continue
            started = time.monotonic()
            logger.info('Step %s: starting', name)
            step(config, conn)
            logger.info('Step %s: done in %.0fs', name, time.monotonic() - started)
    except Exception:
        logger.exception('Pipeline failed')
        return EXIT_FAILURE
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
