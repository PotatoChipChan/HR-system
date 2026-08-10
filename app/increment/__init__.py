from flask import Blueprint

increment_bp = Blueprint('increment', __name__, url_prefix='/increment')

from app.increment.routes import *
