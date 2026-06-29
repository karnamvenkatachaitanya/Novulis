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

# Create a non-root user with UID 1000 (required by Hugging Face Spaces)
RUN useradd -m -u 1000 user && chown -R user:user /app
USER user
ENV PATH="/home/user/.local/bin:$PATH"

# Copy python dependencies and install them
COPY --chown=user requirements.txt ./
RUN pip3 install --no-cache-dir --user -r requirements.txt

# Copy the rest of the backend files
COPY --chown=user main.py auto_healer.py ingest_guidelines.py database_setup.sql ./
COPY --chown=user src/ ./src/
COPY --chown=user visual_baselines.json ./

# Copy the Next.js dashboard
COPY --chown=user dashboard/ ./dashboard/

# Build the Next.js app
WORKDIR /app/dashboard
RUN npm install
RUN npm run build

# Expose port 7860 for Hugging Face Spaces
EXPOSE 7860

# Set environment to production
ENV NODE_ENV=production
ENV PORT=7860
# Direct Playwright to use the pre-installed browsers in the base image
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Start Next.js dashboard
CMD ["npm", "run", "start"]
