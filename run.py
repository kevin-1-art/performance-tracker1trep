"""Entry point.

Development:   python run.py
Production:    gunicorn "run:app" -b 0.0.0.0:8000
CLI commands:  flask --app run init-db | create-user | seed-demo | export-weekly
"""
from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=False, host="127.0.0.1", port=5000)
