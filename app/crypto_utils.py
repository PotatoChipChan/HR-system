"""
Encryption utilities for face encodings using AES-256-GCM
Ensures face data is encrypted at rest in the database
"""
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import os
import base64

# Environment-based or config-based master key
# In production, this should come from environment variables or a key management service
def _get_master_key():
    """
    Get or derive the master encryption key.
    For production, load from secure storage (env vars, HSM, AWS KMS, etc.)
    """
    import os
    key_str = os.environ.get('FACE_ENCRYPTION_KEY')
    if not key_str:
        # Fallback for development - should use a strong, persistent key
        key_str = 'smarthr-face-default-key-change-in-production'
    
    # Derive a 256-bit key from the key string using PBKDF2
    salt = b'smarthr_face_salt'  # In production, use a random salt per deployment
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,  # 256 bits for AES-256
        salt=salt,
        iterations=100000,
    )
    return kdf.derive(key_str.encode())


def encrypt_face_encoding(face_encoding_blob):
    """
    Encrypt a face encoding blob using AES-256-GCM
    
    Args:
        face_encoding_blob: Raw bytes of the face encoding
    
    Returns:
        Base64-encoded string containing: nonce (12 bytes) + ciphertext + tag (16 bytes)
    """
    key = _get_master_key()
    
    # Generate a random 96-bit (12-byte) nonce for GCM
    nonce = os.urandom(12)
    
    # Create cipher and encrypt
    cipher = AESGCM(key)
    ciphertext = cipher.encrypt(nonce, face_encoding_blob, None)
    
    # Return base64-encoded: nonce + ciphertext (includes auth tag)
    encrypted_data = nonce + ciphertext
    return base64.b64encode(encrypted_data).decode('utf-8')


def decrypt_face_encoding(encrypted_blob_b64):
    """
    Decrypt a face encoding blob using AES-256-GCM
    
    Args:
        encrypted_blob_b64: Base64-encoded string from encrypt_face_encoding
    
    Returns:
        Raw bytes of the decrypted face encoding
    
    Raises:
        cryptography.hazmat.primitives.ciphers.aead.InvalidTag: If authentication fails
    """
    try:
        key = _get_master_key()
        
        # Decode from base64
        encrypted_data = base64.b64decode(encrypted_blob_b64)
        
        # Extract nonce (first 12 bytes) and ciphertext (rest)
        nonce = encrypted_data[:12]
        ciphertext = encrypted_data[12:]
        
        # Decrypt
        cipher = AESGCM(key)
        plaintext = cipher.decrypt(nonce, ciphertext, None)
        
        return plaintext
    except Exception as e:
        raise ValueError(f"Failed to decrypt face encoding: {str(e)}")


def is_encrypted(blob_or_string):
    """
    Check if a blob/string appears to be encrypted (base64 string) or raw bytes
    Used for backward compatibility during migration
    
    Args:
        blob_or_string: Either bytes or string
    
    Returns:
        True if it appears to be base64-encoded (encrypted), False if raw bytes
    """
    if isinstance(blob_or_string, bytes):
        return False
    return isinstance(blob_or_string, str)
