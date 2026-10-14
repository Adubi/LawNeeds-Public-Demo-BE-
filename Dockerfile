# Use Python 3.11.6
FROM python:3.11-slim

# Set the working directory in the container.
WORKDIR /usr/src/app

# Install FreeTDS and other dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends freetds-dev freetds-bin && \
    rm -rf /var/lib/apt/lists/*

# Copy the dependencies file to the working directory.
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the content of the local src directory to the working directory.
COPY . .

# Specify the command to run on container start.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5000"]

# Inform Docker that the container listens on the specified network ports at runtime.
EXPOSE 5000
