from flask import Blueprint, g, render_template

from .auth import roles_required
from .db import get_db
from .reporting import build_report
from .utils import last_completed_week_start, today

bp = Blueprint("manager", __name__, url_prefix="/manager")


@bp.route("/")
@roles_required("manager")
def dashboard():
    db = get_db()
    this_week = build_report(db, g.user, today())
    last_week = build_report(db, g.user, last_completed_week_start(today()))
    team = db.execute(
        "SELECT id, full_name, username, weekly_target_minor FROM users "
        "WHERE manager_id = ? AND is_active = 1 ORDER BY full_name", (g.user["id"],)).fetchall()
    return render_template("manager/dashboard.html", this_week=this_week,
                           last_week=last_week, team=team)
