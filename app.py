"""
PSC Stream — desktop entry point.

Starts the local HTTP server on 127.0.0.1 (ephemeral port) and opens a
pywebview desktop window pointed at it. If pywebview is unavailable (or the
window cannot be created), it prints the URL and keeps serving so the app can
be used from any browser instead.
"""

import sys
import traceback

import server


def main():
    srv = server.run_server()
    port = srv.server_address[1]
    url = "http://127.0.0.1:%d/" % port
    mock = " (MOCK MODE)" if server.MOCK else ""
    print("PSC Stream server listening on %s%s" % (url, mock), flush=True)

    try:
        import webview

        window = webview.create_window(
            "PSC Stream",
            url,
            width=1280,
            height=800,
            min_size=(900, 600),
        )
        webview.start()
        return 0
    except Exception:
        print("pywebview could not open a window:", flush=True)
        traceback.print_exc()
        print()
        print("Browser fallback: open this URL manually:", flush=True)
        print("  " + url, flush=True)
        print("Press Ctrl+C to stop.", flush=True)
        try:
            # serve_forever already runs in a daemon thread; just wait here.
            import time

            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
