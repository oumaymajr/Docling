# Use conda base image
FROM continuumio/miniconda3:latest

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Create conda environment
RUN conda create -n docling_env python=3.11 -y

# Make RUN commands use the conda environment
SHELL ["conda", "run", "-n", "docling_env", "/bin/bash", "-c"]

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Create necessary directories
RUN mkdir -p image_cache detected_images/useful detected_images/not_useful

# Expose port
EXPOSE 8001

# Set conda environment activation and run uvicorn
CMD ["conda", "run", "--no-capture-output", "-n", "docling_env", \
     "uvicorn", "docling_apis:app", "--host", "0.0.0.0", "--port", "8001", "--reload"]

