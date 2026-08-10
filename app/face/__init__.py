from flask import Blueprint

face_bp = Blueprint('face', __name__, url_prefix='/face')

# Import routes after blueprint creation
from . import routes
from . import reports

# Initialize face matching cache on import
from .matcher import init_face_cache
try:
    init_face_cache()
except Exception as e:
    print(f"[WARNING] Could not initialize face cache on import: {e}")

