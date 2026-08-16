"""
Face Matching Helper Module
- Load all face encodings from database (decrypt from AES-256-GCM)
- Compare incoming face against all stored encodings
- Return matched employee with confidence score
"""
import numpy as np
import sqlite3
import os
try:
    import face_recognition
except (ImportError, SystemExit):
    face_recognition = None
from app.database import get_db
from app.crypto_utils import decrypt_face_encoding, is_encrypted

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                       'instance', 'smarthr.db')

class FaceMatcherCache:
    """
    In-memory cache of all registered face encodings for fast matching.
    Reduces database queries during real-time matching.
    """
    
    def __init__(self):
        self.encodings = []      # List of face encoding arrays
        self.employee_info = []  # List of corresponding employee dicts
        self.last_updated = None
    
    def load_from_db(self):
        """Load all face encodings from database into memory"""
        try:
            # Use direct SQLite connection (needed for startup before Flask context)
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            
            rows = conn.execute("""
                SELECT f.encoding_id, e.employee_id, e.full_name, 
                       e.branch_id, e.department_id, f.face_encoding_blob
                FROM Face_Encoding f
                JOIN Employee e ON f.employee_id = e.employee_id
                WHERE e.is_active = 1
                ORDER BY e.employee_id
            """).fetchall()
            conn.close()
            
            self.encodings = []
            self.employee_info = []
            
            for row in rows:
                try:
                    # Decrypt the encoding blob
                    encrypted_blob = row['face_encoding_blob']
                    if is_encrypted(encrypted_blob):
                        # It's already encrypted (string)
                        decrypted_bytes = decrypt_face_encoding(encrypted_blob)
                    else:
                        # Fallback for unencrypted legacy data (bytes)
                        decrypted_bytes = encrypted_blob
                    
                    # Convert BLOB back to numpy array
                    encoding = np.frombuffer(decrypted_bytes, dtype=np.float64)
                    self.encodings.append(encoding)
                    
                    self.employee_info.append({
                        'face_id': row['encoding_id'],
                        'employee_id': row['employee_id'],
                        'full_name': row['full_name'],
                        'branch_id': row['branch_id'],
                        'department_id': row['department_id']
                    })
                except Exception as e:
                    print(f"Error loading face for employee {row['employee_id']}: {e}")
                    continue
            
            print(f"[OK] Loaded {len(self.encodings)} face encodings from database")
            return len(self.encodings) > 0
        
        except Exception as e:
            print(f"[ERROR] Loading face encodings: {e}")
            return False
    
    def get_active_count(self):
        """Get count of active face encodings in cache"""
        return len(self.encodings)


# Global cache instance
_face_cache = FaceMatcherCache()

def init_face_cache():
    """Initialize the global face cache (call on app startup)"""
    global _face_cache
    return _face_cache.load_from_db()

def refresh_face_cache():
    """Refresh face cache from database"""
    global _face_cache
    _face_cache.load_from_db()

def match_face(face_encoding, tolerance=0.4):
    """
    Match a face encoding against all registered faces.
    
    Args:
        face_encoding: numpy array (128-dim) from face_recognition
        tolerance: float, threshold for matching (lower = stricter)
                   0.4 = strict/secure (recommended for attendance)
                   0.6 = lenient (more false positives)
    
    Returns:
        {
            'matched': True/False,
            'employee_id': int or None,
            'full_name': str or None,
            'confidence': float (0-1, higher = better match),
            'branch_id': int or None,
            'branch_name': str or None,
            'error': str or None
        }
    """
    global _face_cache
    
    try:
        if face_recognition is None:
            return {
                'matched': False,
                'confidence': 0,
                'error': 'Face recognition library is not available'
            }

        # Check if cache is loaded
        if not _face_cache.encodings:
            return {
                'matched': False,
                'confidence': 0,
                'error': 'No faces registered in system'
            }
        
        # Compare against all registered faces
        distances = face_recognition.face_distance(_face_cache.encodings, face_encoding)
        
        # Find best match
        min_distance = np.min(distances)
        min_index = np.argmin(distances)
        
        # Check if within tolerance
        if min_distance <= tolerance:
            emp_info = _face_cache.employee_info[min_index]
            
            # Get branch name
            conn = get_db()
            branch = conn.execute(
                "SELECT name FROM Branch WHERE branch_id = ?",
                (emp_info['branch_id'],)
            ).fetchone()
            
            branch_name = branch['name'] if branch else 'Unknown'
            
            # Confidence = 1 - distance (inverted)
            confidence = max(0, 1 - min_distance)
            
            return {
                'matched': True,
                'employee_id': emp_info['employee_id'],
                'full_name': emp_info['full_name'],
                'branch_id': emp_info['branch_id'],
                'branch_name': branch_name,
                'department_id': emp_info['department_id'],
                'confidence': round(confidence, 3),
                'distance': round(min_distance, 3)  # For debugging
            }
        else:
            # No match within tolerance
            return {
                'matched': False,
                'confidence': round(1 - min_distance, 3),
                'distance': round(min_distance, 3),
                'error': f'Face not recognized (distance: {min_distance:.2f}, tolerance: {tolerance})'
            }
    
    except Exception as e:
        print(f"Error matching face: {e}")
        return {
            'matched': False,
            'confidence': 0,
            'error': f'Matching error: {str(e)}'
        }

def extract_face_encoding(image_rgb, face_locations=None):
    """
    Extract face encoding from RGB image.
    
    Args:
        image_rgb: numpy array in RGB format
        face_locations: optional pre-computed face locations
    
    Returns:
        (face_encoding, face_count) or (None, 0) if no/multiple faces
    """
    try:
        if face_recognition is None:
            return None, -1
        if face_locations is None:
            face_locations = face_recognition.face_locations(image_rgb, model='hog')
        
        # Validate single face
        if len(face_locations) == 0:
            return None, 0
        if len(face_locations) > 1:
            return None, len(face_locations)
        
        # Extract encoding
        face_encodings = face_recognition.face_encodings(image_rgb, face_locations)
        if not face_encodings:
            return None, 1
        
        return face_encodings[0], 1
    
    except Exception as e:
        print(f"Error extracting face encoding: {e}")
        return None, -1
