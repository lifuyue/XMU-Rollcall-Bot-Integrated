import socket
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from queue import Queue

from flask import Flask, jsonify, render_template, request
from pyngrok import ngrok
from werkzeug.serving import make_server


@dataclass
class QRScanSession:
    sid: str
    url: str
    queue: Queue
    timeout: int


class QRServer:
    def __init__(self, port=5001, ngrok_token="", host="0.0.0.0"):
        self.port = int(port)
        self.ngrok_token = ngrok_token
        self.host = host
        self.public_base_url = ""
        self._lock = threading.Lock()
        self._started = False
        self._sessions = {}
        self._http_server = None
        self._thread = None

        template_dir = Path(__file__).resolve().parent / "templates"
        self._app = Flask(__name__, template_folder=str(template_dir))
        self._register_routes()

    def start(self):
        with self._lock:
            if self._started:
                return
            if not self.ngrok_token:
                raise RuntimeError("qr.ngrok_token is required")

            try:
                ngrok.set_auth_token(self.ngrok_token)
                self._http_server = make_server(
                    self.host,
                    self.port,
                    self._app,
                    threaded=True,
                )
                self._thread = threading.Thread(
                    target=self._http_server.serve_forever,
                    name="xmu-qr-flask",
                    daemon=True,
                )
                self._thread.start()
                self._wait_for_local_server()

                tunnel = ngrok.connect(str(self.port))
                self.public_base_url = self._select_public_base_url(tunnel)
                print(f"[QR] Scanner service started at {self.public_base_url}")
                self._started = True
            except Exception:
                self._shutdown_local_server()
                raise

    def create_session(self, timeout):
        self.start()
        sid = uuid.uuid4().hex
        queue = Queue(maxsize=1)
        timeout = int(timeout)
        with self._lock:
            self._sessions[sid] = queue

        threading.Thread(
            target=self._expire_session,
            args=(sid, timeout),
            name=f"xmu-qr-expire-{sid[:8]}",
            daemon=True,
        ).start()

        return QRScanSession(
            sid=sid,
            url=f"{self.public_base_url}/scan/{sid}",
            queue=queue,
            timeout=timeout,
        )

    def _register_routes(self):
        @self._app.route("/scan/<sid>")
        def scan_page(sid):
            with self._lock:
                exists = sid in self._sessions
            if not exists:
                return "Scanner session does not exist or has expired.", 404
            return render_template("scan.html", sid=sid)

        @self._app.route("/submit/<sid>", methods=["POST"])
        def submit(sid):
            data = request.get_json(force=True, silent=True) or {}
            text = data.get("text")
            if not text:
                return jsonify({"ok": False, "message": "No QR code content received."}), 400

            with self._lock:
                queue = self._sessions.pop(sid, None)
            if queue is None:
                return jsonify({"ok": False, "message": "Scanner session is invalid or expired."}), 404

            queue.put(text)
            return jsonify({"ok": True, "message": "QR code received. You can close this page."})

        @self._app.route("/healthz")
        def healthz():
            return jsonify({"ok": True})

    def _expire_session(self, sid, timeout):
        time.sleep(timeout)
        with self._lock:
            queue = self._sessions.pop(sid, None)
        if queue is not None:
            queue.put(None)
            print(f"[QR] Scanner session {sid} expired.")

    def _wait_for_local_server(self):
        deadline = time.time() + 5
        last_error = None
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError as exc:
                last_error = exc
                time.sleep(0.1)
        raise RuntimeError(f"Flask scanner server did not start on port {self.port}: {last_error}")

    def _shutdown_local_server(self):
        if self._http_server is not None:
            try:
                self._http_server.shutdown()
                self._http_server.server_close()
            except Exception:
                pass
        self._http_server = None
        self._thread = None

    def _select_public_base_url(self, tunnel):
        https_url = None
        http_url = None
        for item in ngrok.get_tunnels():
            public_url = getattr(item, "public_url", "")
            if public_url.startswith("https://"):
                https_url = public_url
            elif public_url.startswith("http://"):
                http_url = public_url

        if https_url:
            return https_url.rstrip("/")
        if http_url:
            print("[QR] Warning: ngrok returned HTTP only; mobile camera access may require HTTPS.")
            return http_url.rstrip("/")
        return tunnel.public_url.rstrip("/")
