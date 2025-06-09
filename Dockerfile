# Use an official, slim Python image as the base
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# --- Install system dependencies, including ffmpeg ---
# This runs as an administrator during the build, so it works!
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && \
    # Clean up the apt cache to keep the final image small
    rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the container first
COPY requirements.txt .

# Install all Python packages from the requirements file
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application's code (like main.py)
COPY . .

# This is the command that will be run to start your bot
CMD ["python", "main.py"]
