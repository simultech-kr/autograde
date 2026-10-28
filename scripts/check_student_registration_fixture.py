"""Temporary, loopback-only membership UI fixture; no production accounts."""
import argparse
from pathlib import Path
import signal
import tempfile
import threading

import check_nextjs_pilot as pilot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18183)
    options = parser.parse_args()
    pilot.WEB_ORIGIN = f'http://127.0.0.1:{options.port}'
    pilot.API_ORIGIN = f'http://127.0.0.1:{options.port + 1}'
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    with tempfile.TemporaryDirectory(prefix='autograde-member-browser-') as root:
        fixture = pilot.PilotFixture(Path(root))
        with pilot.serving(fixture.portal, pilot.WEB_ORIGIN, options.port):
            print(f'Synthetic membership UI ready on {pilot.WEB_ORIGIN}', flush=True)
            while not stop.wait(1):
                pass


if __name__ == '__main__':
    main()
