web: bash -c "python dchub_mcp_server.py --port 8888 & sleep 3 && gunicorn main:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 --max-requests 500 --max-requests-jitter 50"
