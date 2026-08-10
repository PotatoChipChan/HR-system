"""
SmartHR – AI-Powered HR Management System
Entry point: python run.py
Access locally: http://127.0.0.1:5000
Access on LAN:  http://<your-ip>:5000
"""
from dotenv import load_dotenv
import os
load_dotenv()

from app import create_app

app = create_app()

if __name__ == '__main__':
    debug = os.getenv('FLASK_DEBUG', 'False').lower() in ('true', '1', 't')
    app.run(host='0.0.0.0', port=5000, debug=debug, threaded=True)
