# Use Python base image
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    poppler-utils \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Create directory structure
RUN mkdir -p /data/maghrebia/venvs /data/maghrebia/Git/Docling

# Set working directory
WORKDIR /data/maghrebia/Git/Docling

# Copy requirements first for better caching
COPY requirements.txt .

# Create virtual environment at the specified location
RUN python -m venv /data/maghrebia/venvs/doc_venv

# Activate venv and install dependencies
RUN /data/maghrebia/venvs/doc_venv/bin/pip install --upgrade pip && \
    /data/maghrebia/venvs/doc_venv/bin/pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Create necessary directories
RUN mkdir -p image_cache detected_images/useful detected_images/not_useful

# Expose port
EXPOSE 8001

# Activate venv and run uvicorn
CMD ["/data/maghrebia/venvs/doc_venv/bin/uvicorn", "docling_apis:app", "--host", "127.0.0.1", "--port", "8001", "--reload"]

