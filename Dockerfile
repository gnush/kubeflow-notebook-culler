FROM python:3.15-slim

WORKDIR /app

RUN pip install --no-cache-dir kubernetes requests

COPY culler.py .

CMD ["python", "culler.py"]