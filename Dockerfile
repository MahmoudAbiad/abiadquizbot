FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create downloads and logs directories
RUN mkdir -p downloads logs

# Run the bot
CMD ["python", "main.py"]
