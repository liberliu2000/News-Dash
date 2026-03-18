from src.app import app
from src.config import load_settings

if __name__ == "__main__":
    settings = load_settings()
    app.run(host=settings.dashboard_host, port=settings.dashboard_port, debug=settings.dashboard_debug)
