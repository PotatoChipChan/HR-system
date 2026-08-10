from flask import Blueprint

perf_bp = Blueprint('performance', __name__, url_prefix='/performance')

from app.performance import routes