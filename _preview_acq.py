"""Serve just the acquisitions blueprint locally so the page can be looked at.

The portal needs Postgres, which is not available here, but the acquisitions
pages only touch it for projects and folders. This registers the blueprint with
a stubbed database and a fake signed-in admin so the layout and styling can be
checked in a browser rather than inferred from the HTML.

Not part of the app — a local preview harness.
"""
import os
import sys

os.environ.setdefault(
    "ACQ_DATA_DIR",
    r"C:\Users\CarlosSaldierna\OneDrive - Ember Group\Desktop\Acquisitions GIS App\storage")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

from flask import Flask, session                                   # noqa: E402
import acq_routes                                                  # noqa: E402

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config.update(
    SECRET_KEY="preview",
    ACQ_GET_DB=lambda: None,
    ACQ_LOGIN_REQUIRED=(lambda f: f),
    ACQ_ADMIN_REQUIRED=(lambda f: f),
    ACQ_LOG_ACTIVITY=(lambda *a, **k: None),
    ACQ_REFRESH_PAGE_ACCESS=(lambda *a, **k: {"acquisitions": True}),
)
acq_routes.init_app(app)


@app.before_request
def _fake_login():
    session["user_id"] = 1
    session["is_admin"] = True
    session["username"] = "carlos"
    session["display_name"] = "Carlos"


if __name__ == "__main__":
    app.run(port=5099, use_reloader=False, threaded=True)
