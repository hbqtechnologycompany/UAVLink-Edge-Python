from flask import Flask, jsonify
import threading
import logging
from typing import Optional

logger = logging.getLogger("WebServer")

app = Flask(__name__)
stats_ref = None
auth_ref = None

@app.route('/api/status')
def status():
    global stats_ref, auth_ref
    return jsonify({
        'status': 'Running',
        'auth_token': auth_ref.session_token[:20] + "..." if auth_ref and auth_ref.session_token else "none",
        'stats': stats_ref if stats_ref else {}
    })

def start_server(port, stats, auth):
    global stats_ref, auth_ref
    stats_ref = stats
    auth_ref = auth
    logger.info(f"Starting web server on port {port}")
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False), daemon=True).start()
