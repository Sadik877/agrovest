#!/usr/bin/env python3
"""
AgroVest Pro — Startup Script
Initializes the database and starts the Flask development server.

Usage:
    python run.py
"""

from app import app, init_db

if __name__ == '__main__':
    print("=" * 55)
    print("  🌾  AgroVest Pro — Agricultural Investment Platform")
    print("=" * 55)

    with app.app_context():
        init_db()
        print("  ✓  Database initialized")
        print("  ✓  Admin account ready: admin@agrovest.ng / Admin@2024!")
        print("  ✓  Starting server on http://localhost:5000")
        print("=" * 55)

    app.run(debug=True, port=5000, host='0.0.0.0')
