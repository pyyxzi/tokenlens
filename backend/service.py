"""Private analytics sidecar for TokenLens's native Tauri host."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import secrets
import sys
import threading


def main():
    line = sys.stdin.buffer.readline(8193)
    if len(line) > 8192 or not line.endswith(b'\n'):
        raise ValueError('invalid startup handshake')
    handshake = json.loads(line)
    token = handshake.get('token')
    home = handshake.get('home')
    if (not isinstance(token, str) or len(token) != 64
            or any(c not in '0123456789abcdef' for c in token)
            or not isinstance(home, str) or not Path(home).is_absolute()):
        raise ValueError('invalid startup handshake')
    os.environ['TOKENLENS_HOME'] = home
    if not getattr(sys, 'frozen', False):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    logging.getLogger('tokenlens').addHandler(logging.NullHandler())
    logging.getLogger('tokenlens').propagate = False
    from tokenlens.config import Config
    from tokenlens.server import App, ReusableServer, RequestError, make_handler

    app = App(Config())
    base_handler = make_handler(app)

    class PrivateHandler(base_handler):
        server_version = 'TokenLens'

        def check_origin(self):
            provided = self.headers.get_all('X-Kjuan-Secret', [])
            if (len(provided) != 1 or not secrets.compare_digest(provided[0], token)
                    or self.headers.get('Origin') or self.headers.get('Sec-Fetch-Site')
                    or not self.path.startswith('/api/')):
                raise RequestError('request rejected', 403)
            return super().check_origin()

    server = ReusableServer(('127.0.0.1', 0), PrivateHandler)
    lifecycle_lock = threading.Lock()

    def control():
        # The pipe belongs to the native parent. EOF means it has exited.
        while not app.stop_event.is_set():
            raw = sys.stdin.buffer.readline(1025)
            if not raw or len(raw) > 1024:
                break
            try:
                if json.loads(raw).get('shutdown') is True:
                    break
            except (ValueError, AttributeError):
                break
        with lifecycle_lock:
            app.stop_event.set()
            app.tasks.cancel_all()
            app.scanner.cancel()
        server.shutdown()

    worker = threading.Thread(target=server.serve_forever, daemon=True, name='tokenlens-api')
    worker.start()
    threading.Thread(target=control, daemon=True, name='tokenlens-host-pipe').start()
    print(json.dumps({'port': server.server_address[1], 'ready': True}), flush=True)
    # Bind the authenticated API before any background scan can compete with
    # startup queries. A scan failure must not make the private service vanish.
    if app.cfg.get('scan_on_start', True) and not app.stop_event.is_set():
        def start_scan():
            try:
                with lifecycle_lock:
                    if not app.stop_event.is_set():
                        app.scanner.start(force=False)
            except Exception:
                logging.getLogger('tokenlens').error('Startup scan could not start')
        threading.Thread(target=start_scan, daemon=True, name='tokenlens-startup-scan').start()
    try:
        worker.join()
    finally:
        with lifecycle_lock:
            app.stop_event.set()
            app.tasks.cancel_all()
            app.scanner.cancel()
        app.backup.close()
        server.server_close()
        app.scanner.wait(timeout=5)
        app.store.close()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Never serialize arbitrary provider errors, paths or startup secrets.
        print('TokenLens analytics service could not start', file=sys.stderr, flush=True)
        sys.exit(1)
