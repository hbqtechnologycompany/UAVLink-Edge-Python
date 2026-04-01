import logging
import time
import signal
import sys
from config import Config
from auth_client import AuthClient
from forwarder import Forwarder
from web_server import start_server
from video_streamer import VideoStreamer

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m--%d %H:%M:%S'
)
logger = logging.getLogger("MAIN")

video_streamer = None

def signal_handler(sig, frame):
    logger.info("👋 Shutting down...")
    if video_streamer:
        video_streamer.stop()
    sys.exit(0)

def main():
    parser = argparse.ArgumentParser(description="UAVLink-Edge (Python Version)")
    parser.add_argument("--register", action="store_true", help="Register drone with server to get SecretKey")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info("🚀 Starting UAVLink-Edge (Python Version) on Pi 5")
    
    # WiFi-only mode enforced by design (no 4G module interaction code added)
    logger.info("Network mode: WiFi-only (4G disabled per request)")
    
    # Load configuration
    try:
        cfg = Config("config.yaml")
        logger.info("Configuration loaded successfully")
    except Exception as e:
        logger.fatal(f"Failed to load configuration: {e}")
        sys.exit(1)

    # Initialize components
    auth = AuthClient(
        cfg.auth.get('host'),
        cfg.auth.get('port'),
        cfg.auth.get('uuid'),
        cfg.auth.get('shared_secret'),
        cfg.auth.get('keepalive_interval', 30)
    )
    
    fwd = Forwarder(cfg, auth)
    
    # Start components
    if args.register:
        logger.info("Registering drone...")
        if auth.register():
            logger.info("✅ Registration successful. You can now run without --register")
        else:
            logger.error("❌ Registration failed")
        sys.exit(0)

    # Start web server
    start_server(cfg.web.get('port', 8080), fwd.stats, auth)

    # Authentication
    logger.info("Authenticating via public TCP...")
    if not auth.start():
        logger.warning("Initial authentication failed. Will retry in background.")
    else:
        logger.info("✅ Successfully authenticated")

    # Start forwarder
    if not fwd.start():
        logger.fatal("Failed to start forwarder")
        sys.exit(1)

    # Start Video Stream
    global video_streamer
    video_streamer = VideoStreamer(cfg)
    video_streamer.start()

    logger.info("UAVLink-Edge running. Press Ctrl+C to stop.")
    
    # Keep main thread alive
    while True:
        time.sleep(1)

if __name__ == "__main__":
    main()
