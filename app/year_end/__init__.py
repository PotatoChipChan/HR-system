from flask import Blueprint
year_end_bp = Blueprint('year_end', __name__)
from app.year_end import routes
