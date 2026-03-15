FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py .
COPY routes/ ./routes/
COPY *.py ./

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV MCP_TRANSPORT=streamable-http

# Expose port
EXPOSE 8080

# Run the Flask application
CMD ["python", "main.py"]
