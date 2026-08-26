"""Person-facing pages. Session cookie only."""
from flask import Blueprint, render_template
from flask_login import current_user, login_required

from app.db import db_session
from app.models import User
from app.auth.decorators import admin_required

bp = Blueprint('dashboard', __name__)


@bp.get('/')
@login_required
def index():
    return render_template('index.html')


@bp.get('/admin/team')
@admin_required
def team():
    """The admin's view of everyone. A worker reaching this gets 404, not 403 —
    they should not learn the page exists."""
    people = db_session.query(User).order_by(User.name).all()
    return render_template('team.html', people=people)
