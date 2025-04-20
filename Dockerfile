# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Install system dependencies
# ffmpeg is required by yt-dlp for merging formats
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the container
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
# --no-cache-dir reduces image size
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container
COPY . .

# Create the directory for downloads inside the image
# This directory will be mounted as a volume
RUN mkdir /app/downloads

# Make port 5000 available to the world outside this container
EXPOSE 5000

# Define environment variables (optional)
ENV FLASK_APP=app.py
ENV FLASK_RUN_HOST=0.0.0.0
# Consider using a production server like gunicorn or waitress
# ENV PYTHONUNBUFFERED=1 # Ensures logs print directly

# Run app.py when the container launches
# Use waitress for a more production-ready server
# RUN pip install waitress # Add waitress to requirements.txt if using this
# CMD ["waitress-serve", "--host", "0.0.0.0", "--port", "5000", "app:app"]

# Or use Flask's built-in server (less performant, okay for simple use)
CMD ["python", "app.py"]

