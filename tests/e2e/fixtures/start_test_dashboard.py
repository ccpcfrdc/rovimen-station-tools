"""Launch the dashboard in test mode for Playwright E2E tests.

Calls create_app() directly (not main()), so no SSH tunnels, no
background pollers, and no station connectivity required. Station API
routes will return error/offline responses, which the frontend handles
gracefully.
"""

import os
import sys
import tempfile
from pathlib import Path

FIXTURES = Path(__file__).parent
PROJECT = FIXTURES.parent.parent.parent

tmp = Path(tempfile.mkdtemp(prefix="rovimen_e2e_"))

os.environ["ROVIMEN_USERS_PATH"] = str(FIXTURES / "users.yaml")
os.environ["ROVIMEN_SECRET_KEY"] = "e2e-test-secret-key-not-for-production"
os.environ["ROVIMEN_COOKIE_SECURE"] = "0"
os.environ["RATELIMIT_ENABLED"] = "0"
os.environ["THUMB_CACHE_DIR"] = str(tmp / "thumb_cache")
os.environ["ROVIMEN_CACHE_PATH"] = str(tmp / "cache")
os.environ["ROVIMEN_COMPILATIONS_OUT_PATH"] = str(tmp / "compilations")
os.environ["ROVIMEN_KNOWN_HOSTS"] = str(tmp / "known_hosts")
os.environ["ROVIMEN_AUDIT_LOG_PATH"] = str(tmp / "audit.log")
os.environ["ROVIMEN_ACTIVITY_LOG_PATH"] = str(tmp / "activity.log")
# Keep the docstring's promise: build the routes/caches but start none of the
# long-lived poller/scheduler daemons (no SSH tunnels, no station reach needed).
os.environ["ROVIMEN_DISABLE_BACKGROUND"] = "1"

sys.path.insert(0, str(PROJECT / "dashboard"))

from rovimen_dashboard import load_config, create_app  # noqa: E402

import security  # noqa: E402

security._LOGIN_THRESHOLD = 10_000
security._LOGIN_LOCKOUT_SECONDS = 0

original_init_limiter = security.init_limiter
def _noop_limiter(app):
    lim = original_init_limiter(app)
    lim.enabled = False
    return lim
security.init_limiter = _noop_limiter

config_path = FIXTURES / "dashboard_config.yaml"
config = load_config(config_path)
app = create_app(config, config_path)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5111, debug=False)
