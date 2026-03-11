web: python -c "from app import init_db; init_db(); print('DB initialized')" && gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120 --log-level debug
