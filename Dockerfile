# Use the official Microsoft Playwright image which comes with Node.js and browser dependencies pre-installed
FROM mcr.microsoft.com/playwright:v1.40.0-jammy

# Set up working directory
WORKDIR /app

# Install Python 3, pip, and venv
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Copy python dependencies and install them
COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

# Install Playwright browser binaries for python
RUN python3 -m playwright install chromium

# Copy the rest of the backend files
COPY main.py auto_healer.py ingest_guidelines.py database_setup.sql ./
COPY src/ ./src/
COPY visual_baselines.json ./

# Copy the Next.js dashboard
COPY dashboard/ ./dashboard/

# Build the Next.js app
WORKDIR /app/dashboard
RUN npm install
RUN npm run build

# Expose port 7860 for Hugging Face Spaces
EXPOSE 7860

# Set environment to production
ENV NODE_ENV=production
ENV PORT=7860

# Start Next.js dashboard
CMD ["npm", "run", "start"]
