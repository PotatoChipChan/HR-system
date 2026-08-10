from flask import Blueprint

bonus_bp = Blueprint('bonus', __name__, url_prefix='/bonus')

from app.bonus.routes import *
